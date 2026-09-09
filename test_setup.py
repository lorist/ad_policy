"""Tests for the setup wizard, and a guard that .env.example stays usable as
its question source."""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "setup"))
import wizard  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
EXAMPLE = os.path.join(ROOT, ".env.example")


def test_example_parses_and_every_question_has_text():
    variables = wizard.parse_example(EXAMPLE)
    names = [v.name for v in variables]
    assert len(names) == len(set(names)), "duplicate variable in .env.example"
    for v in variables:
        assert v.section, "%s has no #! section" % v.name
        assert v.help, "%s has no question text above it" % v.name
    # the variables the app and proxy actually read must all be present
    for required in ("LDAP_HOST", "LDAP_USER", "LDAP_PASSWORD", "LDAP_BASE_DN",
                     "POLICY_USER", "POLICY_PASSWORD", "TLS_MODE", "MATTERMOST_POLICY_URL"):
        assert required in names


def test_directives_are_read():
    by_name = {v.name: v for v in wizard.parse_example(EXAMPLE)}
    assert by_name["LDAP_HOST"].required and by_name["LDAP_HOST"].type == "hostname"
    assert by_name["LDAP_PASSWORD"].secret
    assert by_name["TLS_MODE"].choices == ["self-signed", "provided", "off"]
    assert by_name["PROXY_HOSTNAME"].when == ("TLS_MODE", "self-signed")
    assert by_name["POLICY_PASSWORD"].generate == "password"
    assert by_name["LOG_PII"].advanced


@pytest.mark.parametrize("type_,value,ok", [
    ("int", "636", True), ("int", "six", False),
    ("bool", "Yes", True), ("bool", "maybe", False),
    ("url", "https://mm.example.com/plugins/x/api/", True), ("url", "mm.example.com", False),
    ("hostname", "dc01.example.com", True), ("hostname", "10.10.0.10", True),
    ("hostname", "https://dc01", False),
])
def test_validate_types(type_, value, ok):
    var = wizard.Var("X", "", [], "s", {"type": type_})
    _, err = wizard.validate(var, value)
    assert (err is None) == ok, err


def test_bool_is_normalized():
    var = wizard.Var("X", "", [], "s", {"type": "bool"})
    assert wizard.validate(var, "Y")[0] == "true"
    assert wizard.validate(var, "off")[0] == "false"


def test_required_rejects_blank():
    var = wizard.Var("X", "", [], "s", {"required": True})
    assert wizard.validate(var, "  ")[1]


def test_quote_round_trips_awkward_values():
    for value in ("plain", "with space", "p@ss$word!", "it's", 'say "hi"', "back\\slash"):
        line = "K=%s" % wizard.quote(value)
        assert wizard.unquote(line.partition("=")[2]) == value, line


def run_wizard(tmp_path, *sets, env_exists=False):
    env = tmp_path / ".env"
    cmd = [sys.executable, os.path.join(ROOT, "setup", "wizard.py"), "--non-interactive",
           "--env", str(env), "--example", EXAMPLE, "--skip-cert-check"]
    for s in sets:
        cmd += ["--set", s]
    return subprocess.run(cmd, capture_output=True, text=True), env


def test_non_interactive_writes_env_with_generated_password(tmp_path):
    result, env = run_wizard(tmp_path,
                             "LDAP_HOST=dc01.example.com", "LDAP_USER=svc", "LDAP_PASSWORD=pw",
                             "LDAP_BASE_DN=DC=example,DC=com", "PROXY_HOSTNAME=policy.example.com")
    assert result.returncode == 0, result.stderr
    values = wizard.read_env(str(env))
    assert values["LDAP_HOST"] == "dc01.example.com"
    assert values["TLS_MODE"] == "self-signed"
    assert len(values["POLICY_PASSWORD"]) >= 16          # generated
    assert values["AVATAR_CACHE_TTL"] == "300"           # advanced default kept
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_non_interactive_fails_on_missing_required(tmp_path):
    result, _ = run_wizard(tmp_path, "LDAP_USER=svc")
    assert result.returncode != 0
    assert "LDAP_HOST" in result.stderr


def test_rerun_keeps_existing_values_and_unknown_keys(tmp_path):
    _, env = run_wizard(tmp_path, "LDAP_HOST=dc01", "LDAP_USER=svc", "LDAP_PASSWORD=pw",
                        "LDAP_BASE_DN=DC=x", "PROXY_HOSTNAME=p.example.com")
    first = wizard.read_env(str(env))
    with open(env, "a") as fh:
        fh.write("CUSTOM_THING=kept\n")
    result, env = run_wizard(tmp_path, "LDAP_HOST=dc02")
    assert result.returncode == 0, result.stderr
    second = wizard.read_env(str(env))
    assert second["LDAP_HOST"] == "dc02"
    assert second["POLICY_PASSWORD"] == first["POLICY_PASSWORD"]
    assert second["CUSTOM_THING"] == "kept"


def test_conditional_question_is_skipped_but_still_written(tmp_path):
    result, env = run_wizard(tmp_path, "LDAP_HOST=dc01", "LDAP_USER=svc", "LDAP_PASSWORD=pw",
                             "LDAP_BASE_DN=DC=x", "TLS_MODE=off")
    assert result.returncode == 0, result.stderr
    values = wizard.read_env(str(env))
    assert values["TLS_MODE"] == "off"
    assert "PROXY_HOSTNAME" in values   # default carried, not required when TLS is off


def test_cert_check_reports_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(wizard, "CERTS_DIR", str(tmp_path))
    problems = wizard.check_certificates({"TLS_MODE": "provided", "LDAP_VALIDATE_CERT": "true"}, False)
    assert any("fullchain.pem" in p for p in problems)
    assert any("ad-ca.pem" in p for p in problems)
    assert wizard.check_certificates({"TLS_MODE": "self-signed", "LDAP_VALIDATE_CERT": "false"}, False) == []
