#!/usr/bin/env python3
"""Interactive setup for ad_policy and its proxy.

The questions are read from .env.example: the comment above each variable is
the question text, and "#!" lines are directives:

    #! section: <title>        start a new group of questions
    #! required                value may not be empty
    #! secret                  ask without echo, never print the value
    #! type: int|bool|url|hostname
    #! choices: a|b|c          one of a fixed set
    #! when: VAR=value         only ask when an earlier answer matches
    #! generate: password      offer a random password when none is set
    #! advanced                only asked with --advanced; default kept otherwise

Answers are written to .env (mode 600). Re-running the wizard offers the
current values as defaults, so it doubles as an editor. Standard library only,
so it runs on any host Python 3.8+ or inside a python:alpine container.
"""
import argparse
import datetime
import getpass
import ipaddress
import os
import re
import secrets
import ssl
import sys
import tempfile
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PATH = os.path.join(ROOT, ".env.example")
ENV_PATH = os.path.join(ROOT, ".env")
CERTS_DIR = os.path.join(ROOT, "certs")

TRUE_WORDS = ("1", "true", "yes", "y", "on")
FALSE_WORDS = ("0", "false", "no", "n", "off")


class Var:
    def __init__(self, name, default, help_lines, section, meta):
        self.name = name
        self.default = default
        self.help = help_lines
        self.section = section
        self.required = "required" in meta
        self.secret = "secret" in meta
        self.advanced = "advanced" in meta
        self.type = meta.get("type", "string")
        self.choices = meta["choices"].split("|") if "choices" in meta else None
        self.generate = meta.get("generate")
        self.when = None
        if "when" in meta:
            k, _, v = meta["when"].partition("=")
            self.when = (k.strip(), v.strip())

    def __repr__(self):
        return "Var(%s)" % self.name


# --- parsing -----------------------------------------------------------------

def unquote(value):
    """Inverse of quote(): strip surrounding quotes; unescape inside double quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        if value[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return value


def parse_example(path=EXAMPLE_PATH):
    """Return the ordered list of Var described by an annotated .env.example."""
    variables = []
    section = None
    help_lines = []
    meta = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if line.startswith("#!"):
                directive = line[2:].strip()
                key, _, value = directive.partition(":")
                key, value = key.strip(), value.strip()
                if key == "section":
                    section = value
                    help_lines, meta = [], {}
                else:
                    meta[key] = value if value else True
            elif line.startswith("#"):
                help_lines.append(line.lstrip("#").strip())
            elif not line.strip():
                help_lines, meta = [], {}
            else:
                name, _, default = line.partition("=")
                variables.append(Var(name.strip(), unquote(default), help_lines, section, meta))
                help_lines, meta = [], {}
    return variables


def read_env(path=ENV_PATH):
    """Read NAME=value pairs from an existing .env. Missing file -> {}."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            values[name.strip()] = unquote(value)
    return values


# --- validation --------------------------------------------------------------

def is_hostname(value):
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return re.fullmatch(r"(?=.{1,253}$)([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", value) is not None


def normalize_bool(value):
    v = str(value).strip().lower()
    if v in TRUE_WORDS:
        return "true"
    if v in FALSE_WORDS:
        return "false"
    return None


def validate(var, value):
    """Return (normalized_value, error_message_or_None)."""
    value = value.strip()
    if not value:
        if var.required:
            return value, "a value is required"
        return value, None
    if var.type == "int":
        if not re.fullmatch(r"-?\d+", value):
            return value, "must be a whole number"
    elif var.type == "bool":
        norm = normalize_bool(value)
        if norm is None:
            return value, "answer y or n"
        value = norm
    elif var.type == "url":
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return value, "must be a full URL starting with http:// or https://"
    elif var.type == "hostname":
        if not is_hostname(value):
            return value, "must be a hostname or IP address (no scheme, no path)"
    if var.choices and value not in var.choices:
        return value, "must be one of: " + ", ".join(var.choices)
    return value, None


def condition_met(var, answers):
    if var.when is None:
        return True
    name, wanted = var.when
    actual = answers.get(name, "")
    if normalize_bool(wanted) is not None and normalize_bool(actual) is not None:
        return normalize_bool(wanted) == normalize_bool(actual)
    return actual == wanted


# --- prompting ---------------------------------------------------------------

def show_section(title):
    print()
    print(title)
    print("-" * len(title))


def prompt_value(var, default, generated=False):
    """Ask once; return the raw string the user gave (empty = keep default)."""
    if var.type == "bool":
        shown = "Y/n" if normalize_bool(default) == "true" else "y/N"
        raw = input("%s [%s]: " % (var.name, shown))
        return raw if raw.strip() else default
    if var.choices:
        print("  options: " + ", ".join(var.choices))
    if var.secret:
        if generated:
            hint = "[Enter = use a generated password]"
        else:
            hint = "[keep current]" if default else "[required]" if var.required else "[blank]"
        raw = getpass.getpass("%s %s: " % (var.name, hint))
        return raw if raw else default
    hint = "[%s]" % default if default else "[required]" if var.required else "[blank]"
    raw = input("%s %s: " % (var.name, hint))
    return raw if raw.strip() else default


def ask(var, default, interactive, generated=False):
    if not interactive:
        value, err = validate(var, default)
        if err:
            sys.exit("%s: %s (set it with --set %s=...)" % (var.name, err, var.name))
        return value
    for line in var.help:
        print("  " + line)
    while True:
        raw = prompt_value(var, default, generated)
        value, err = validate(var, raw)
        if not err:
            return value
        print("  ! %s" % err)


# --- certificate checks -------------------------------------------------------

def check_certificates(answers, interactive):
    """Verify the certificate files an answer set depends on. Returns [] when
    everything is fine, otherwise a list of problems."""
    problems = []
    if answers.get("TLS_MODE") == "provided":
        cert = os.path.join(CERTS_DIR, "proxy", "fullchain.pem")
        key = os.path.join(CERTS_DIR, "proxy", "privkey.pem")
        if not os.path.exists(cert) or not os.path.exists(key):
            problems.append("TLS_MODE=provided needs certs/proxy/fullchain.pem and certs/proxy/privkey.pem")
        else:
            try:
                ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(cert, key)
            except (ssl.SSLError, OSError) as e:
                problems.append("certs/proxy/fullchain.pem and privkey.pem do not load together: %s" % e)
    if normalize_bool(answers.get("LDAP_VALIDATE_CERT", "false")) == "true":
        ca = os.path.join(CERTS_DIR, "ad-ca.pem")
        if not os.path.exists(ca):
            problems.append("LDAP_VALIDATE_CERT=true needs the domain controller's CA certificate at certs/ad-ca.pem")
        else:
            try:
                ssl.create_default_context().load_verify_locations(ca)
            except (ssl.SSLError, OSError) as e:
                problems.append("certs/ad-ca.pem is not a readable PEM certificate: %s" % e)
    return problems


# --- writing -----------------------------------------------------------------

def quote(value):
    if re.fullmatch(r"[A-Za-z0-9_./:@+=,\\-]*", value):
        return value
    if "'" not in value:
        return "'" + value + "'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env(variables, answers, extra, path=ENV_PATH):
    lines = [
        "# Written by ./setup.sh on %s. Re-run ./setup.sh to change values." % datetime.date.today().isoformat(),
        "# Questions and defaults live in .env.example.",
    ]
    section = None
    for var in variables:
        if var.section != section:
            section = var.section
            lines += ["", "# --- %s" % section]
        lines.append("%s=%s" % (var.name, quote(answers.get(var.name, ""))))
    if extra:
        lines += ["", "# --- Kept from the previous .env (not in .env.example)"]
        lines += ["%s=%s" % (k, quote(v)) for k, v in extra.items()]
    lines.append("")
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


# --- main ----------------------------------------------------------------------

def collect(variables, current, overrides, interactive, advanced):
    answers = {}
    section = None
    for var in variables:
        default = overrides.get(var.name, current.get(var.name, var.default))
        generated = False
        if var.generate == "password" and not default:
            default = secrets.token_urlsafe(18)
            generated = True
        if var.name in overrides or not condition_met(var, answers) or (var.advanced and not advanced):
            value, err = validate(var, default) if (var.name in overrides or condition_met(var, answers)) else (default, None)
            if err:
                sys.exit("%s: %s" % (var.name, err))
            answers[var.name] = value
            continue
        if interactive and var.section != section:
            section = var.section
            show_section(section)
        answers[var.name] = ask(var, default, interactive, generated)
    return answers


def summary(answers):
    host = answers.get("PROXY_HOSTNAME") or "<this server>"
    scheme = "http" if answers.get("TLS_MODE") == "off" else "https"
    print()
    print("Written .env. Next steps")
    print("------------------------")
    print("1. Start the containers:      docker compose up -d --build")
    print("2. Check them:                ./setup.sh check [<a user with a photo>]")
    if answers.get("TLS_MODE") == "self-signed":
        print("3. Trust the certificate:     ./setup.sh cert  (upload it to Pexip under")
        print("                              Certificates > Trusted CA certificates)")
    print("4. Pexip policy profile:      URL %s://%s/   user %s" % (scheme, host, answers.get("POLICY_USER")))
    if answers.get("MATTERMOST_POLICY_URL"):
        print("   Enable 'service configuration' and 'participant avatar' on the Mattermost")
        print("   location; 'participant avatar' only elsewhere. See INSTALL.md.")
    else:
        print("   Enable 'participant avatar' only. See INSTALL.md.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write .env by answering questions from .env.example.")
    ap.add_argument("--non-interactive", action="store_true",
                    help="take values from the existing .env, --set, or defaults; fail on anything missing")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help="preset an answer (repeatable)")
    ap.add_argument("--advanced", action="store_true", help="also ask the advanced questions")
    ap.add_argument("--env", default=ENV_PATH, help=argparse.SUPPRESS)
    ap.add_argument("--example", default=EXAMPLE_PATH, help=argparse.SUPPRESS)
    ap.add_argument("--skip-cert-check", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    overrides = {}
    for item in args.set:
        name, sep, value = item.partition("=")
        if not sep:
            ap.error("--set expects NAME=VALUE, got %r" % item)
        overrides[name.strip()] = value

    variables = parse_example(args.example)
    current = read_env(args.env)
    interactive = not args.non_interactive
    if interactive and not sys.stdin.isatty():
        interactive = False

    if interactive:
        print("ad_policy setup. Press Enter to accept the value in brackets.")
        if current:
            print("Existing values from .env are offered as defaults.")

    answers = collect(variables, current, overrides, interactive, args.advanced)

    if not args.skip_cert_check:
        while True:
            problems = check_certificates(answers, interactive)
            if not problems:
                break
            for p in problems:
                print("  ! " + p)
            if not interactive:
                sys.exit(1)
            input("  Put the files in place, then press Enter to check again (Ctrl-C to abort). ")

    extra = {k: v for k, v in current.items() if k not in {v.name for v in variables}}
    write_env(variables, answers, extra, args.env)
    if interactive:
        summary(answers)
    else:
        print("wrote %s" % args.env)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted; .env was not changed.")
        sys.exit(130)
