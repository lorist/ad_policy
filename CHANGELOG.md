# Changelog

## 1.0.0 — 2026-09-09

First release intended for customer installation. Compared with the original
single-script version:

### Install and operate
- `./setup.sh` wizard writes `.env` from the questions in `.env.example`
  (required, secret, typed and conditional answers; re-run to edit;
  `--non-interactive` for automation).
- `./setup.sh check [user]` verifies containers, ports, credentials, the
  domain controller, and an optional real lookup with the log reason.
- `./setup.sh csr` / `./setup.sh install-cert` request and install a
  certificate from your own CA (AD Certificate Services supported directly);
  self-signed and externally terminated TLS are the alternatives.
- `INSTALL.md` walks through the whole install including the Pexip side.
- Runs as two containers: `ad_policy` (internal only) behind the `proxy`
  (TLS, credential check, routing).

### Mattermost
- One Pexip policy profile can serve both the Mattermost plugin and avatar
  lookups: the proxy routes service configuration to Mattermost and
  participant avatars to `ad_policy`.
- Example local policy that gives Mattermost meetings a theme.

### Lookups and diagnostics
- Matches by sAMAccountName, userPrincipalName, display name, email and
  telephone number.
- Every miss is logged with its reason (no entry, no photo, bind failure,
  rejected search); LDAP failures are never cached, definitive misses are
  cached for `AVATAR_CACHE_TTL` seconds.
- `/healthz` reports the build version; `/healthz/ldap` probes TCP and TLS to
  the domain controller.
- Optional validation of the domain controller's certificate against a
  supplied CA (`certs/ad-ca.pem`).

### Security
- LDAP filter input is escaped; avatar dimensions are clamped.
- Participant identities are hashed in logs unless `LOG_PII=true`.
- The container runs as a non-root user; the proxy never receives the LDAP
  credentials.

### Build and CI
- Python 3.14, Flask 3.1, Pillow 12, gunicorn 26, ldap3 2.9.
- GitHub Actions: tests and container smoke test on every PR, proxy TLS-mode
  and CSR round-trip checks, Snyk scanning and Azure deployment when their
  secrets are configured, Dependabot updates.
