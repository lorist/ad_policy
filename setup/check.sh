#!/bin/sh
# Post-install checks, run from inside the containers so they work on any
# host. Usage: ./setup.sh check [<identifier of a user with a photo>]
cd "$(dirname "$0")/.."

envval() {
  grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- \
    | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'
}

fail=0
ok()   { printf '  ok    %s\n' "$1"; }
bad()  { printf '  FAIL  %s\n' "$1"; fail=1; }
info() { printf '        %s\n' "$1"; }

if [ ! -f .env ]; then
  echo "No .env found. Run ./setup.sh first."; exit 1
fi
USER_="$(envval POLICY_USER)"; PASS_="$(envval POLICY_PASSWORD)"
MODE="$(envval TLS_MODE)"; HOST_="$(envval PROXY_HOSTNAME)"
AUTH="Authorization: Basic $(printf '%s:%s' "$USER_" "$PASS_" | base64 | tr -d '\n')"

echo "Containers"
for svc in ad_policy proxy; do
  state="$(docker compose ps --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null | awk -v s="$svc" '$1==s {print $2" "$3}')"
  case "$state" in
    running*healthy*|running\ ) ok "$svc is $state" ;;
    running*) ok "$svc is $state" ;;
    "") bad "$svc is not running (docker compose up -d --build)" ;;
    *) bad "$svc is $state" ;;
  esac
done

wget_in_proxy() { docker compose exec -T proxy wget -q -O - -T 10 "$@" 2>/dev/null; }
# HTTP status of a request made from inside the proxy container. busybox wget
# reports non-2xx as "wget: server returned error: HTTP/1.1 401 ..." on
# stderr, so pull the status out of whichever line carries "HTTP/x.y NNN".
status_in_proxy() {
  docker compose exec -T proxy sh -c "wget -q -O /dev/null -S -T 15 $1 2>&1" \
    | grep -oE 'HTTP/[0-9.]+ [0-9]{3}' | awk '{print $2}' | tail -1
}

echo "Proxy"
if body="$(wget_in_proxy http://127.0.0.1/healthz)"; then ok "answers on :80 ($body)"; else bad "does not answer on :80 (docker compose logs proxy)"; fi
if [ "$MODE" != "off" ]; then
  if [ -f certs/proxy/fullchain.pem ]; then
    ok "certificate present at certs/proxy/fullchain.pem"
    [ "$MODE" = "self-signed" ] && info "self-signed: upload it to Pexip (./setup.sh cert) if not done yet"
  else
    bad "no certificate at certs/proxy/fullchain.pem (docker compose logs proxy)"
  fi
  if docker compose exec -T proxy wget -q -O /dev/null -T 10 --no-check-certificate https://127.0.0.1/healthz 2>/dev/null; then
    ok "answers on :443 (TLS)"
  else
    bad "does not answer on :443"
  fi
fi

echo "Credentials and Active Directory"
code="$(status_in_proxy http://127.0.0.1/healthz/ldap)"
if [ "$code" = "401" ]; then ok "avatar path requires credentials"; else bad "avatar path answered ${code:-nothing} without credentials"; fi
code="$(status_in_proxy "--header='$AUTH' http://127.0.0.1/healthz/ldap")"
case "$code" in
  401) bad "POLICY_USER/POLICY_PASSWORD rejected by the proxy (docker compose logs proxy)" ;;
  "")  bad "no answer from the proxy for /healthz/ldap" ;;
  *)   ok "POLICY_USER/POLICY_PASSWORD accepted" ;;
esac
# Ask ad_policy directly for the DC probe; its body explains a 503.
body="$(docker compose exec -T ad_policy python -c '
import urllib.request, urllib.error
try:
    r = urllib.request.urlopen("http://127.0.0.1:5000/healthz/ldap", timeout=15); print(r.read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
except Exception as e:
    print("{\"error\": \"%s\"}" % e)
' 2>/dev/null)"
case "$body" in
  *'"tcp": "ok"'*'"tls": "ok"'*|*'"tcp": "ok"'*'"tls": "skipped"'*) ok "domain controller reachable: $body" ;;
  *'"tls": "error"'*) bad "TLS handshake with the domain controller failed: $body" ;;
  *) bad "domain controller not reachable on the LDAP port: $body" ;;
esac

if [ -n "${1:-}" ]; then
  echo "Avatar lookup for '$1'"
  code="$(status_in_proxy "--header='$AUTH' 'http://127.0.0.1/policy/v1/participant/avatar/$1?width=100&height=100'")"
  case "$code" in
    200) ok "photo returned (HTTP 200)" ;;
    404) bad "no photo (HTTP 404) — the ad_policy log line below says why" ;;
    *)   bad "unexpected HTTP ${code:-(no response)}" ;;
  esac
  docker compose logs --no-log-prefix --tail 30 ad_policy 2>/dev/null | grep -E 'pexavatar - (INFO|WARNING|ERROR)' | grep -vE 'health|Starting' | tail -4 | sed 's/^/        /'
fi

echo
if [ "$fail" = 0 ]; then
  scheme=https; [ "$MODE" = "off" ] && scheme=http
  echo "All checks passed. Pexip policy profile URL: $scheme://${HOST_:-<this server>}/  user: $USER_"
else
  echo "Some checks failed. See the hints above and INSTALL.md → Troubleshooting."
  exit 1
fi
