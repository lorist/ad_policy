#!/bin/sh
# Drop the HTTPS server block when no certificate is mounted, so the
# container still starts (HTTP only) behind a TLS-terminating platform.
if [ ! -f /etc/nginx/certs/fullchain.pem ] || [ ! -f /etc/nginx/certs/privkey.pem ]; then
    rm -f /etc/nginx/templates/tls.conf.template
    echo "proxy: no certificate in /etc/nginx/certs — listening on :80 only"
else
    echo "proxy: certificate found — listening on :80 and :443"
fi
