#!/bin/sh
# Generate a self-signed certificate for the proxy's HTTPS listener.
# Usage: ./make-self-signed-cert.sh policy.example.com
# Upload the resulting fullchain.pem to Pexip under
# Certificates > Trusted CA certificates so the Conferencing Nodes trust it.
set -e
HOST="${1:?usage: $0 <hostname>}"
DIR="$(dirname "$0")/certs"
openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout "$DIR/privkey.pem" -out "$DIR/fullchain.pem" \
    -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST"
chmod 600 "$DIR/privkey.pem"
echo "wrote $DIR/fullchain.pem and $DIR/privkey.pem for $HOST"
