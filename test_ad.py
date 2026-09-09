"""Unit tests for the avatar policy server's pure helpers.

These cover input classification (searchFilter), avatar size clamping
(parse_dimension), and LDAP filter escaping — none of which touch the network,
so they run without a live AD.
"""
import pytest
from ldap3.utils.conv import escape_filter_chars

import ad


# --- searchFilter: input classification -----------------------------------

def test_searchfilter_email():
    search, filt = ad.searchFilter("walter@example.com")
    assert search == "walter@example.com"
    assert filt == "(mail={0}*)"


def test_searchfilter_numeric_normalizes_plus():
    # A bare number gains the leading '+' the AD telephoneNumber format expects.
    search, filt = ad.searchFilter("15551234")
    assert search == "+15551234"
    assert filt == "(telephoneNumber={0}*)"


def test_searchfilter_numeric_keeps_plus():
    search, _ = ad.searchFilter("+15551234")
    assert search == "+15551234"


def test_searchfilter_display_name():
    search, filt = ad.searchFilter("walter kurtz")
    assert search == "walter kurtz"
    assert filt == "(|(sAMAccountName={0})(userPrincipalName={0}*)(displayName={0}*))"


def test_searchfilter_sam_account_name():
    search, filt = ad.searchFilter("walter.kurtz")
    assert search == "walter.kurtz"
    assert "sAMAccountName={0}" in filt


def test_searchfilter_unsupported_returns_none():
    # Input starting with a non-word char (e.g. an injection probe) is rejected.
    assert ad.searchFilter("*)(objectClass=*") is None


# --- LDAP injection escaping ----------------------------------------------

def test_escape_neutralizes_metacharacters():
    escaped = escape_filter_chars("*)(objectClass=*")
    assert "*" not in escaped
    assert "(" not in escaped
    assert ")" not in escaped


def test_escaped_name_filter_has_no_raw_wildcard_from_input():
    # The name branch matches inputs starting with a word char, so 'walter*'
    # reaches the filter; escaping must neutralize the injected '*' while the
    # template's own trailing '*' wildcards remain.
    search, filt = ad.searchFilter("walter*")
    built = filt.format(escape_filter_chars(search))
    assert "walter*" not in built
    assert "walter\\2a" in built
    # template wildcards are still present
    assert "userPrincipalName=walter\\2a*" in built


# --- parse_dimension: clamping --------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, ad.DEFAULT_DIMENSION),
    ("abc", ad.DEFAULT_DIMENSION),
    ("128", 128),
    ("999999", ad.MAX_DIMENSION),
    ("0", 1),
    ("-5", 1),
])
def test_parse_dimension(raw, expected):
    assert ad.parse_dimension(raw) == expected


# --- redact: PII handling in logs -----------------------------------------

def test_redact_hides_identity_by_default(monkeypatch):
    monkeypatch.setattr(ad, "LOG_PII", False)
    out = ad.redact("walter@example.com")
    assert out.startswith("id:")
    assert "walter" not in out
    # stable for the same input
    assert out == ad.redact("walter@example.com")
    # distinct inputs hash differently
    assert out != ad.redact("kurtz@example.com")


def test_redact_passthrough_when_enabled(monkeypatch):
    monkeypatch.setattr(ad, "LOG_PII", True)
    assert ad.redact("walter@example.com") == "walter@example.com"


# --- find_ad_users: every miss says why, and only real answers are cached ---

from ldap3.core.exceptions import LDAPBindError


class FakeConnection:
    """Stands in for ldap3.Connection: a context manager whose search() returns
    `ok` and whose response_to_json() returns the canned `entries`."""

    def __init__(self, entries=None, ok=True, result=None):
        self.entries = entries or []
        self.ok = ok
        self.result = result or {}
        self.search_kwargs = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def search(self, **kwargs):
        self.search_kwargs = kwargs
        return self.ok

    def response_to_json(self):
        import json
        return json.dumps({"entries": self.entries})


@pytest.fixture
def lookup(monkeypatch):
    """Clear the cache, enable it, and return a helper that installs a fake
    connection (or a bind failure) and runs a lookup with INFO logs captured."""
    monkeypatch.setattr(ad, "AVATAR_CACHE_TTL", 300)
    ad._cache.clear()

    def run(caplog, participant="walter@example.com", conn=None, bind_error=None):
        def fake_ldap_connection():
            if bind_error is not None:
                raise bind_error
            return conn

        monkeypatch.setattr(ad, "ldap_connection", fake_ldap_connection)
        with caplog.at_level("INFO", logger="pexavatar"):
            return ad.find_ad_users(participant)

    return run


def test_lookup_returns_photo_and_caches_hit(lookup, caplog):
    conn = FakeConnection(entries=[{
        "dn": "CN=walter,OU=People,DC=test,DC=invalid",
        "attributes": {"thumbnailPhoto": {"encoded": "QUJD"}},
    }])
    assert lookup(caplog, conn=conn) == "QUJD"
    assert conn.search_kwargs["search_base"] == ad.LDAP_BASE_DN
    assert ad.cache_lookup("walter@example.com") == "QUJD"


def test_lookup_no_entry_logs_reason_and_caches_miss(lookup, caplog):
    assert lookup(caplog, conn=FakeConnection(entries=[])) is None
    assert "No directory entry matched" in caplog.text
    assert ad.LDAP_BASE_DN in caplog.text
    # a definitive "no such user" answer is cached as a miss (None, not _MISS)
    assert ad.cache_lookup("walter@example.com") is None


def test_lookup_entry_without_photo_logs_reason(lookup, caplog):
    # ldap3 returns [] for a requested attribute the entry doesn't carry
    conn = FakeConnection(entries=[{
        "dn": "CN=walter,OU=People,DC=test,DC=invalid",
        "attributes": {"thumbnailPhoto": []},
    }])
    assert lookup(caplog, conn=conn) is None
    assert "has no thumbnailPhoto" in caplog.text
    assert ad.cache_lookup("walter@example.com") is None


def test_lookup_bind_failure_logs_error_and_is_not_cached(lookup, caplog):
    err = LDAPBindError("automaticBindNotSuccessful: invalidCredentials")
    assert lookup(caplog, bind_error=err) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors, "bind failure must be logged at ERROR"
    assert "bind to" in errors[0].getMessage()
    assert "invalidCredentials" in errors[0].getMessage()
    # a failure to ask the directory must not pin a 404 for the cache TTL
    assert ad.cache_lookup("walter@example.com") is ad._MISS


def test_lookup_rejected_search_logs_result_and_is_not_cached(lookup, caplog):
    # e.g. a mistyped LDAP_BASE_DN: ldap3 returns False rather than raising
    conn = FakeConnection(ok=False, result={"description": "noSuchObject",
                                            "message": "0000208D: NameErr"})
    assert lookup(caplog, conn=conn) is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert errors and "noSuchObject" in errors[0].getMessage()
    assert ad.cache_lookup("walter@example.com") is ad._MISS


def test_lookup_logs_never_contain_raw_identity(lookup, caplog, monkeypatch):
    monkeypatch.setattr(ad, "LOG_PII", False)
    conn = FakeConnection(entries=[{
        "dn": "CN=walter kurtz,OU=People,DC=test,DC=invalid",
        "attributes": {"thumbnailPhoto": []},
    }])
    lookup(caplog, conn=conn)
    assert "walter" not in caplog.text


# --- /healthz: liveness with build version ---------------------------------

def test_healthz_reports_version(monkeypatch):
    monkeypatch.setattr(ad, "APP_VERSION", "abc1234")
    resp = ad.app.test_client().get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok", "version": "abc1234"}
