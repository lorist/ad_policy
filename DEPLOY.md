# Deployment

## Azure App Service — Web App for Containers

This uses the project's `Dockerfile`, so the image you run in Azure is identical
to the one you run locally with `docker compose`.

### Prerequisites

- The [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
  installed and signed in (`az login`).
- A subscription with permission to create resource groups, an Azure Container
  Registry, and an App Service plan.

### Gotcha: the listening port

App Service for containers routes traffic to port **80** by default, but the
`Dockerfile` listens on **5000**. You must set the `WEBSITES_PORT` app setting to
`5000` (done below) or the site returns 504s.

### 1. Select the subscription

If your account has more than one subscription, set the one you want to deploy
into before creating any resources:

```bash
# List the subscriptions you can access
az account list --output table

# Set the active subscription (by name or id)
az account set --subscription "My Subscription Name"

# Confirm which one is active
az account show --query '{name:name, id:id}' --output table
```

The commands below create resources in whatever subscription is active here. To
make it explicit, add a variable and pass `--subscription "$SUB"` to each `az`
command:

```bash
SUB=$(az account show --query id -o tsv)
```

### 2. Build and deploy

```bash
# Variables — replace with your own values
RG=<resource-group>
LOC=<azure-region>             # e.g. australiaeast
ACR=myacr$RANDOM              # must be globally unique, lowercase
PLAN=<app-service-plan>
APP=<app-name>                 # becomes <app-name>.azurewebsites.net

# Resource group + container registry
az group create -n $RG -l $LOC
az acr create -n $ACR -g $RG --sku Basic --admin-enabled true

# Build the image in ACR (no local Docker needed)
az acr build -r $ACR -t ad_policy:latest .

# Linux App Service plan (B1 is the cheapest that runs containers well)
az appservice plan create -n $PLAN -g $RG --is-linux --sku B1

# Create the Web App from the ACR image
az webapp create -n $APP -g $RG -p $PLAN \
  --deployment-container-image-name $ACR.azurecr.io/ad_policy:latest

# Wire ACR credentials so the app can pull the image
az webapp config container set -n $APP -g $RG \
  --docker-custom-image-name $ACR.azurecr.io/ad_policy:latest \
  --docker-registry-server-url https://$ACR.azurecr.io \
  --docker-registry-server-user $(az acr credential show -n $ACR --query username -o tsv) \
  --docker-registry-server-password $(az acr credential show -n $ACR --query 'passwords[0].value' -o tsv)
```

### 3. Configure app settings

Your `.env` file is not baked into the image, so set the real values as App
Settings. `WEBSITES_PORT` tells App Service which container port to route to, and
`LOG_FILE` points at a persistent, log-stream-visible path.

```bash
az webapp config appsettings set -n $APP -g $RG --settings \
  WEBSITES_PORT=5000 \
  LDAP_HOST=your_ad_server.com \
  LDAP_USER=service_accnt \
  LDAP_PASSWORD='your_real_password' \
  LDAP_BASE_DN='OU=People,DC=custom,DC=com' \
  LDAP_PORT=636 \
  LDAP_USE_SSL=true \
  LDAP_VALIDATE_CERT=false
```

> **Do not** set `LOG_FILE` to a path like `/home/LogFiles/...`. That directory
> does not exist inside a custom container unless you also enable persistent
> storage (`WEBSITES_ENABLE_APP_SERVICE_STORAGE=true`), and pointing the app at a
> missing directory crashes it on startup (gunicorn exit code 3). Leave `LOG_FILE`
> unset — the app logs to stdout, which Azure captures in the log stream.

### 4. Verify

```
https://<app-name>.azurewebsites.net/policy/v1/participant/avatar/<display-name>?width=200&height=200
```

Tail logs with `az webapp log tail -n $APP -g $RG`.

### Redeploying

After code changes, rebuild the image and restart:

```bash
az acr build -r $ACR -t ad_policy:latest .

# Force App Service to re-pull the image. Because the tag stays `latest`, a bare
# `az webapp restart` often reuses the cached image and your changes don't ship.
# Re-setting the container config guarantees a fresh pull.
az webapp config container set -n $APP -g $RG \
  --docker-custom-image-name $ACR.azurecr.io/ad_policy:latest \
  --docker-registry-server-url https://$ACR.azurecr.io

az webapp restart -n $APP -g $RG
```

> **Confirm the new image is actually live.** With a reused `latest` tag it's
> easy to think you redeployed when you didn't. Tail the logs
> (`az webapp log tail -n $APP -g $RG`) and check `/healthz` responds, or
> compare the running behaviour against your change. Alternatively, build with a
> unique tag per release (e.g. `-t ad_policy:$(git rev-parse --short HEAD)`) and
> point the container config at that tag to sidestep cache ambiguity entirely.

### Notes for this app

- **LDAP reachability.** The server makes outbound LDAPS (636) connections to your
  domain controller. App Service can only reach it if the DC is internet-reachable,
  or you add [VNet integration](https://learn.microsoft.com/azure/app-service/overview-vnet-integration)
  plus a VPN/ExpressRoute to the network where AD lives. This is the most likely
  thing to break in a private/on-prem AD setup.
- **Logging.** The app logs to stdout, so logs appear in `az webapp log tail` and
  the portal Log stream with no extra config. If you want a persistent log file as
  well, enable `WEBSITES_ENABLE_APP_SERVICE_STORAGE=true` and then set
  `LOG_FILE=/home/LogFiles/pexavatar.log` — only with storage enabled does that
  directory exist.
