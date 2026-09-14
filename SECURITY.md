# Security

MGX ARC GUI manages baseboard management controllers (BMCs) on lab hardware. Treat it as **privileged infrastructure software**, not a public web application.

## Scope

- The GUI proxies Redfish (HTTPS) and SSH from your browser session to BMCs and optional OS hosts.
- Credentials are sent per request (Direct Connect) or resolved from environment variables (Fleet Health).
- BMC TLS certificates are not verified (lab self-signed certs).

## Never commit secrets

| Secret type | Safe approach |
| ----------- | ------------- |
| BMC passwords | Environment variables (`MGX_ARC_BMC_PASSWORD`) or login form at runtime |
| OS host passwords | Entered in the GUI per session; never stored in repo |
| Deploy / Spark SSH | `SPARK_USER`, `SPARK_PASSWORD` env vars only (`deploy/push_to_spark.py`) |
| API tokens, VPN creds | Local `.env` (gitignored) |

**Do not commit:** `.env`, `.flaskenv`, credential files, `deploy_backup/`, or snapshot databases.

## Lab default credentials in source

The repository includes **well-known OpenBMC lab defaults** for developer convenience:

- `root` / `0penBmc` — enforced in `app.py` (`REQUIRED_BMC_USER`, `REQUIRED_BMC_PASSWORD`) and referenced in UI helpers
- OS SSH prefills (`aerial` / `nvidia`) and scope VNC prefills (`labuser`) in `static/js/app.js`

These are **not production secrets** — they are common defaults on lab OpenBMC images. Before deploying outside a closed lab:

1. Change `REQUIRED_BMC_USER` / `REQUIRED_BMC_PASSWORD` in `app.py` to match your authorized credentials, or refactor to environment variables.
2. Update UI prefills in `app.js` if needed.
3. Restrict network access to the GUI host.

## Network exposure

**Do not expose BMC management ports to the public internet.**

| Safe | Unsafe |
| ---- | ------ |
| Run GUI on a host inside the lab management network | Port-forward BMC SSH/Redfish to WAN |
| Access GUI via VPN, Tailscale, or controlled tunnel | Leave `:4281` / `:4282` open on a public IP |
| Browser → GUI → BMC (proxy model) | Browser → BMC directly from untrusted networks |

See [deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md) for DGX Spark deployment patterns.

## Reporting vulnerabilities

If you discover a security issue, please report it privately to the repository maintainer rather than opening a public issue with exploit details.

## Responsible use checklist

- [ ] Real passwords only in environment variables or runtime login — not in git
- [ ] `.gitignore` excludes `.env`, firmware binaries, and deploy backups
- [ ] GUI reachable only over trusted networks
- [ ] Lab default credentials reviewed/changed for your deployment
- [ ] Firmware flash operations tested on authorized hardware only
