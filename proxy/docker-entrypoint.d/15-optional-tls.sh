#!/bin/sh
# Decide whether the HTTPS server block is rendered, according to TLS_MODE:
#   self-signed  generate a certificate into /etc/nginx/certs on first start
#   provided     require a mounted certificate; refuse to start without it
#   off          HTTP only (something in front terminates TLS)
#   auto         (default) HTTPS if a certificate is mounted, else HTTP only
set -e
CERT=/etc/nginx/certs/fullchain.pem
KEY=/etc/nginx/certs/privkey.pem
MODE="${TLS_MODE:-auto}"

have_cert() { [ -f "$CERT" ] && [ -f "$KEY" ]; }

case "$MODE" in
  off)
    rm -f /etc/nginx/templates/tls.conf.template
    echo "proxy: TLS_MODE=off — listening on :80 only"
    ;;
  self-signed)
    if ! have_cert; then
      HOST="${PROXY_HOSTNAME:-localhost}"
      # SAN must say IP: for an address and DNS: for a name.
      if echo "$HOST" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then SAN="IP:$HOST"; else SAN="DNS:$HOST"; fi
      mkdir -p /etc/nginx/certs
      # Keep an existing key: a CSR made with ./setup.sh csr may be waiting
      # at the CA, and its certificate must still match this key later.
      if [ ! -f "$KEY" ]; then
        openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out "$KEY" 2>/dev/null
        chmod 600 "$KEY"
      fi
      openssl req -x509 -key "$KEY" -sha256 -days 825 -out "$CERT" \
        -subj "/CN=$HOST" -addext "subjectAltName=$SAN" 2>/dev/null
      echo "proxy: generated a self-signed certificate for $HOST"
      echo "proxy: upload certs/proxy/fullchain.pem to Pexip (Certificates > Trusted CA certificates)"
    fi
    echo "proxy: TLS_MODE=self-signed — listening on :80 and :443"
    ;;
  provided)
    if ! have_cert; then
      echo "proxy: TLS_MODE=provided but $CERT / $KEY are missing — refusing to start" >&2
      exit 1
    fi
    echo "proxy: TLS_MODE=provided — listening on :80 and :443"
    ;;
  *)
    if have_cert; then
      echo "proxy: certificate found — listening on :80 and :443"
    else
      rm -f /etc/nginx/templates/tls.conf.template
      echo "proxy: no certificate in /etc/nginx/certs — listening on :80 only"
    fi
    ;;
esac
