#!/bin/sh
# Certificate helpers for the proxy: create a CSR for your own CA (for
# example Active Directory Certificate Services) and install what comes back.
#
#   ./setup.sh csr [--new-key] [name ...]   write certs/proxy/request.csr
#   ./setup.sh install-cert <cert> [chain ...]
#
# Uses the host's openssl when present, otherwise the alpine/openssl image.
set -e
cd "$(dirname "$0")/.."
DIR=certs/proxy
KEY=$DIR/privkey.pem
CSR=$DIR/request.csr
CHAIN=$DIR/fullchain.pem
IN=$DIR/incoming

envval() {
  grep -E "^$1=" .env 2>/dev/null | head -1 | cut -d= -f2- \
    | sed -e "s/^'\(.*\)'$/\1/" -e 's/^"\(.*\)"$/\1/'
}
fail() { echo "$*" >&2; exit 1; }

# Run openssl with paths relative to certs/ so host and container agree.
ossl() {
  if command -v openssl >/dev/null 2>&1; then
    (cd certs && openssl "$@")
  else
    docker run --rm -u "$(id -u):$(id -g)" -v "$(pwd)/certs:/certs" -w /certs alpine/openssl "$@"
  fi
}

is_ip() { echo "$1" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; }

# --- csr ---------------------------------------------------------------------

cmd_csr() {
  new_key=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --new-key) new_key=1; shift ;;
      *) break ;;
    esac
  done
  cn="${1:-$(envval PROXY_HOSTNAME)}"
  if [ -z "$cn" ]; then
    echo "Give the DNS name Pexip will use, or set PROXY_HOSTNAME with ./setup.sh first." >&2
    fail "  ./setup.sh csr policy.example.com [extra.name ...]"
  fi
  [ $# -gt 0 ] && shift
  san=""
  for n in "$cn" "$@"; do
    if is_ip "$n"; then entry="IP:$n"; else entry="DNS:$n"; fi
    san="${san:+$san,}$entry"
  done

  mkdir -p "$DIR"
  if [ -f "$KEY" ] && [ "$new_key" = 0 ]; then
    echo "Using the existing private key $KEY (pass --new-key for a fresh one)."
  else
    ossl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out proxy/privkey.pem 2>/dev/null
    chmod 600 "$KEY"
    echo "Generated a new 2048-bit RSA private key at $KEY"
  fi
  ossl req -new -key proxy/privkey.pem -out proxy/request.csr -sha256 \
    -subj "/CN=$cn" -addext "subjectAltName=$san" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"
  echo "Wrote $CSR for CN=$cn with SAN $san"
  echo
  echo "Submit it to your CA and ask for a server (TLS web server) certificate."
  echo "Active Directory Certificate Services, from any domain-joined Windows machine:"
  echo "  certreq -submit -attrib \"CertificateTemplate:WebServer\" request.csr signed.cer"
  echo "  (or paste the text below into https://<ca-server>/certsrv > Request a certificate"
  echo "   > advanced > 'Web Server' template, then download 'Base 64 encoded')"
  echo "Then install the result here with:"
  echo "  ./setup.sh install-cert signed.cer [certnew.p7b or the CA chain file]"
  echo
  cat "$CSR"
}

# --- install-cert ---------------------------------------------------------------

# Print every certificate in a file as PEM, whatever its format
# (PEM certificate or bundle, DER certificate, PKCS#7 in PEM or DER).
# $1 is relative to certs/.
to_pem() {
  f="$1"
  if grep -q 'BEGIN CERTIFICATE' "certs/$f"; then
    ossl crl2pkcs7 -nocrl -certfile "$f" | ossl pkcs7 -print_certs
  elif grep -q 'BEGIN PKCS7' "certs/$f"; then
    ossl pkcs7 -in "$f" -print_certs
  elif ossl x509 -in "$f" -inform DER -noout 2>/dev/null; then
    ossl x509 -in "$f" -inform DER
  elif ossl pkcs7 -in "$f" -inform DER -print_certs -noout 2>/dev/null; then
    ossl pkcs7 -in "$f" -inform DER -print_certs
  else
    fail "Cannot read $f as a certificate (PEM, DER, or PKCS#7)."
  fi
}

# Split a PEM stream on stdin into $IN/part<N>.pem, numbering on from $1.
# Prints the last number used. Plain shell: awk differs across platforms.
split_parts() {
  i="$1"; out=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      "-----BEGIN CERTIFICATE-----") i=$((i + 1)); out="$IN/part$i.pem"; : > "$out" ;;
    esac
    [ -n "$out" ] && printf '%s\n' "$line" >> "$out"
    case "$line" in
      "-----END CERTIFICATE-----") out="" ;;
    esac
  done
  echo "$i"
}

# Subject or issuer of a certificate in $IN, without the "subject=" prefix.
cert_field() { ossl x509 -in "proxy/incoming/$(basename "$1")" -noout "-$2" | sed "s/^$2=//"; }

cmd_install() {
  [ $# -ge 1 ] || fail "usage: ./setup.sh install-cert <signed cert> [chain files ...]"
  [ -f "$KEY" ] || fail "No private key at $KEY. Run ./setup.sh csr first."
  for f in "$@"; do [ -f "$f" ] || fail "No such file: $f"; done

  rm -rf "$IN"; mkdir -p "$IN"
  n=0
  for f in "$@"; do
    cp "$f" "$IN/input"
    to_pem proxy/incoming/input | grep -vE '^(subject|issuer)=' > "$IN/all.pem"
    n=$(split_parts "$n" < "$IN/all.pem")
  done
  [ "$n" -ge 1 ] || fail "No certificates found in the given files."

  # part1 is the server certificate; it must be for OUR key.
  leaf="$IN/part1.pem"
  key_pub="$(ossl pkey -in proxy/privkey.pem -pubout 2>/dev/null)"
  cert_pub="$(ossl x509 -in proxy/incoming/part1.pem -pubkey -noout)"
  if [ "$key_pub" != "$cert_pub" ]; then
    rm -rf "$IN"
    echo "That certificate was not issued for $KEY (public keys differ)." >&2
    fail "If you generated a new CSR since, submit the current request.csr again."
  fi
  leaf_subject="$(cert_field "$leaf" subject)"
  leaf_issuer="$(cert_field "$leaf" issuer)"
  echo "Server certificate: $leaf_subject (issued by $leaf_issuer)"

  tmp="$IN/fullchain.pem"
  cat "$leaf" > "$tmp"
  issuer_found=0
  i=2
  while [ "$i" -le "$n" ]; do
    part="$IN/part$i.pem"
    subj="$(cert_field "$part" subject)"
    iss="$(cert_field "$part" issuer)"
    if [ "$subj" = "$iss" ]; then
      echo "Skipping root CA (not served; Pexip must trust it): $subj"
      [ "$subj" = "$leaf_issuer" ] && issuer_found=1
    elif cmp -s "$part" "$leaf"; then
      :   # the server certificate again
    else
      cat "$part" >> "$tmp"
      echo "Added intermediate CA: $subj"
      [ "$subj" = "$leaf_issuer" ] && issuer_found=1
    fi
    i=$((i + 1))
  done
  if [ "$issuer_found" = 0 ]; then
    echo "Note: the issuer ($leaf_issuer) was not among the files given."
    echo "      Fine if it is the root CA; otherwise also pass the intermediate CA certificate."
  fi

  mv "$tmp" "$CHAIN"
  chmod 644 "$CHAIN"
  rm -rf "$IN"
  echo "Wrote $CHAIN ($(grep -c 'BEGIN CERTIFICATE' "$CHAIN") certificate(s))"
  ossl x509 -in proxy/fullchain.pem -noout -enddate -ext subjectAltName 2>/dev/null | sed 's/^/  /'

  # Switch the proxy to the provided certificate (the wizard re-validates the pair).
  if [ -f .env ]; then
    ./setup.sh --non-interactive --set TLS_MODE=provided >/dev/null
    echo "Set TLS_MODE=provided in .env"
    if docker compose ps --status running --services 2>/dev/null | grep -q '^proxy$'; then
      docker compose up -d proxy >/dev/null 2>&1 && echo "Restarted the proxy with the new certificate"
    else
      echo "Start or restart with: docker compose up -d"
    fi
  else
    echo "No .env yet: run ./setup.sh and choose TLS_MODE=provided"
  fi
  echo
  echo "Pexip must trust the CA that issued this certificate. If the Conferencing"
  echo "Nodes do not already trust it, upload the CA's root (and intermediate)"
  echo "certificates under Certificates > Trusted CA certificates."
}

case "${1:-}" in
  csr) shift; cmd_csr "$@" ;;
  install-cert) shift; cmd_install "$@" ;;
  *) fail "usage: $0 csr [--new-key] [name ...] | install-cert <cert> [chain ...]" ;;
esac
