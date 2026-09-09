# ad_policy

[![Deploy](https://github.com/lorist/ad_policy/actions/workflows/deploy.yml/badge.svg)](https://github.com/lorist/ad_policy/actions/workflows/deploy.yml)
[![Security](https://github.com/lorist/ad_policy/actions/workflows/security.yml/badge.svg)](https://github.com/lorist/ad_policy/actions/workflows/security.yml)

A Pexip external policy server that serves participant avatars by looking up the
`thumbnailPhoto` attribute in Active Directory / LDAP.

Point your Pexip policy profile at `http://<ip of policy>:5000`.

## Configuration

Configuration is read from environment variables (a `.env` file is supported).
Copy the example and edit it with your AD details:

```
cp .env.example .env
```

| Variable             | Description                     | Default                      |
| -------------------- | ------------------------------- | ---------------------------- |
| `LDAP_HOST`          | AD / LDAP server hostname       | `your_ad_server.com`         |
| `LDAP_USER`          | Bind/service account            | `service_accnt`              |
| `LDAP_PASSWORD`      | Bind account password           | `password`                   |
| `LDAP_BASE_DN`       | Base DN to search               | `OU=People,DC=custom,DC=com` |
| `LDAP_PORT`          | LDAP port                       | `636`                        |
| `LDAP_USE_SSL`       | Use LDAPS                        | `true`                       |
| `LDAP_VALIDATE_CERT` | Validate the server certificate | `false`                      |
| `LOG_FILE`           | Log file path                   | `pexavatar.log`              |
| `LOG_PII`            | Log raw participant identities (else hashed) | `false`         |
| `AVATAR_CACHE_TTL`   | Lookup cache TTL in seconds (`0` disables) | `300`             |
| `AVATAR_MAX_DIMENSION` | Max avatar width/height         | `512`                      |
| `AVATAR_DEFAULT_DIMENSION` | Default width/height when unset | `300`                |

## Run with Docker (recommended)

```
docker compose up -d --build
```

The service listens on port `5000`. Logs are written to `./logs`.

## Deploy to Azure

To deploy to Azure App Service (Web App for Containers), see [DEPLOY.md](DEPLOY.md).

Pushes to `master` are deployed automatically by GitHub Actions once CI passes,
and every PR is scanned with Snyk. The one-time secrets setup is in
[DEPLOY.md → CI/CD](DEPLOY.md#cicd-github-actions).

## Run locally (without Docker)

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
gunicorn --bind 0.0.0.0:5000 wsgi:app
```

For development you can also run `python ad.py` directly.

## Tests

```
pip install -r requirements-dev.txt
pytest
```

The tests cover input classification, avatar-size clamping, LDAP filter
escaping, the lookup's miss/failure handling (with a fake connection), and the
health endpoint. They do not require a live AD.
