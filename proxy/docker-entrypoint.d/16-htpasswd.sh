#!/bin/sh
# Build the htpasswd file nginx uses for the avatar paths from
# POLICY_USER / POLICY_PASSWORD. These MUST equal the credentials generated
# by the Mattermost plugin, because the Pexip policy profile carries a single
# username/password that is sent to every request type.
set -e
if [ -z "$POLICY_USER" ] || [ -z "$POLICY_PASSWORD" ]; then
    echo "proxy: POLICY_USER / POLICY_PASSWORD not set — avatar requests will get 401" >&2
    : > /etc/nginx/pexip.htpasswd
    exit 0
fi
printf '%s:%s\n' "$POLICY_USER" "$(openssl passwd -apr1 "$POLICY_PASSWORD")" > /etc/nginx/pexip.htpasswd
# Workers run as the nginx user, so the file must be group-readable.
chown root:nginx /etc/nginx/pexip.htpasswd && chmod 640 /etc/nginx/pexip.htpasswd
echo "proxy: avatar auth enabled for user '$POLICY_USER'"
