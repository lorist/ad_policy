# ad_policy

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

## Run with Docker (recommended)

```
docker compose up -d --build
```

The service listens on port `5000`. Logs are written to `./logs`.

## Deploy to Azure

To deploy to Azure App Service (Web App for Containers), see [DEPLOY.md](DEPLOY.md).

## Run locally (without Docker)

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
gunicorn --bind 0.0.0.0:5000 wsgi:app
```

For development you can also run `python ad.py` directly.




