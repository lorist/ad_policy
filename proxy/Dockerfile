# Pexip policy front door: routes each policy request type to the service
# that implements it (Mattermost plugin or ad_policy). See proxy/README.md.
FROM nginx:1.28-alpine

# openssl is only used at startup to hash POLICY_PASSWORD for auth_basic.
RUN apk add --no-cache openssl

# Only these variables are substituted in the templates; nginx's own
# $request_uri etc. are left alone.
ENV NGINX_ENVSUBST_FILTER='^(MATTERMOST_POLICY_URL|AVATAR_UPSTREAM_URL|DNS_RESOLVER)$'

COPY templates/            /etc/nginx/templates/
COPY docker-entrypoint.d/  /docker-entrypoint.d/

EXPOSE 80 443
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1/healthz || exit 1
