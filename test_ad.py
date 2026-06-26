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
