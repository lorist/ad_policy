# Installing ad_policy

ad_policy serves participant photos from Active Directory to Pexip Infinity,
and can share its policy URL with the Pexip Mattermost integration. It runs as
two Docker containers:

| Container | Role |
| --- | --- |
| `ad_policy` | Looks up `thumbnailPhoto` in AD and returns a JPEG. Not reachable from outside. |
| `proxy` | The address Pexip talks to. Terminates TLS, checks the policy credentials, and routes Mattermost requests to the Mattermost plugin. |

## Before you start

- A Linux host (or VM) with **Docker Engine 24+** and the **Docker Compose plugin**
  (`docker compose version` works). 1 vCPU and 1 GB RAM are plenty.
- Network access from that host to a **domain controller on port 636** (LDAPS).
- Network access from the **Pexip Conferencing Nodes to this host on port 443**
  (or 80 if something in front of it does TLS).
- A **DNS name** for this host that the Conferencing Nodes can resolve. It goes
  into the certificate and the Pexip policy profile.
- An AD **service account** that can read user objects (read-only is enough).
- If you use Mattermost: the **policy URL, username and password** shown in the
  Pexip plugin's settings in the Mattermost System Console.

## 1. Get the files

```
git clone https://github.com/lorist/ad_policy.git
cd ad_policy
```

(or unpack the release archive and `cd` into it).

## 2. Answer the setup questions

```
./setup.sh
```

The wizard asks for the AD connection, the optional Mattermost URL, the
credentials Pexip will send, and how TLS should work. Press Enter to accept
the value in brackets. It writes `.env` (readable only by you) and prints the
next steps. Run it again at any time to change an answer; existing values are
offered as defaults.

Choosing TLS:

| Answer | When to use it | What you have to do |
| --- | --- | --- |
| `self-signed` (default) | Most installs. | After first start, upload the generated certificate to Pexip (step 4). |
| `provided` | You have a certificate from your CA. | Put `fullchain.pem` and `privkey.pem` in `certs/proxy/` before running the wizard; it checks they match. |
| `off` | A load balancer or reverse proxy in front does TLS. | Point it at port 80 of this host. |

If you turned on **Verify the domain controller's certificate**, copy the CA
certificate that issued the DC's certificate to `certs/ad-ca.pem` first; the
wizard checks it parses.

## 3. Start and check

```
docker compose up -d --build
./setup.sh check
```

`check` confirms both containers are up, the proxy answers on 80 and 443, the
credentials work, and the domain controller is reachable. Give it an
identifier of a user who has a photo in AD to test a real lookup:

```
./setup.sh check jsmith
```

A user is found by sAMAccountName (`jsmith`), display name (`Jane Smith`),
email (`jsmith@example.com`) or telephone number.

## 4. Configure Pexip Infinity

**Trust the certificate** (self-signed only): run `./setup.sh cert`, then in the
Pexip Management Node go to *Platform > Certificates > Trusted CA certificates*,
import the file it names, and wait a minute for the nodes to pick it up.

**Create the policy profile** under *Call control > Policy profiles*:

| Field | Value |
| --- | --- |
| Name | e.g. `AD avatars` |
| URL | `https://<the DNS name you entered>/` |
| Username / password | the `POLICY_USER` / `POLICY_PASSWORD` you chose |
| Enable participant avatar lookup | on |
| Enable external service configuration lookup | on **only** if you use Mattermost |
| everything else | off |

**Assign it** to each system location under *Platform > Locations* > *Policy
profile*.

**With Mattermost:** create two profiles with the same URL and credentials.
The one with *service configuration* also enabled goes on the location that
handles Mattermost calls; the avatar-only one goes on every other location.
To give Mattermost meetings a specific theme, add
`proxy/local_policy_mattermost_theme.jinja` as the first profile's local
service-configuration policy. The Mattermost event sink is configured
separately and points at Mattermost directly, not at this server.

**Test:** join a meeting from the Pexip web app as a user who has a photo in
AD. Their photo should appear in the participant list.

## Day-to-day

| Task | Command |
| --- | --- |
| Change a setting | `./setup.sh` then `docker compose up -d` |
| Update to a new version | `git pull` then `docker compose up -d --build` |
| Watch the logs | `docker compose logs -f` |
| Stop / start | `docker compose down` / `docker compose up -d` |
| Renew a provided certificate | replace the files in `certs/proxy/` then `docker compose restart proxy` |

Lookups are cached for five minutes (`AVATAR_CACHE_TTL`), so a photo added in
AD can take that long to appear.

## Troubleshooting

Run `./setup.sh check <user>` first; it points at the failing stage. Then:

| Symptom | Where to look |
| --- | --- |
| Pexip logs "External policy request failed" | The URL in that log line must be this server's, and the certificate must be trusted by Pexip. Check *Status > Support log* filtered on `externalpolicy`. |
| `401` in the proxy log | `POLICY_USER` / `POLICY_PASSWORD` differ from the Pexip profile (or from the Mattermost plugin). |
| `No directory entry matched` in `docker compose logs ad_policy` | Wrong identifier, or the user is outside `LDAP_BASE_DN`. |
| `matched ... but has no thumbnailPhoto` | The AD user has no photo. |
| `LDAP lookup ... failed: bind ...` | Service account name or password wrong. |
| `LDAP lookup ... failed: ... noSuchObject` | `LDAP_BASE_DN` does not exist. |
| `healthz/ldap` shows `"tcp": "error"` | Port 636 to the DC is blocked from this host. |
| Photo missing only for some users | Their email has a dot before the `@` and their UPN differs from it; look them up by sAMAccountName instead. |

To see raw identifiers in the log while debugging, run `./setup.sh --advanced`
and set `LOG_PII` to yes, then set it back afterwards.
