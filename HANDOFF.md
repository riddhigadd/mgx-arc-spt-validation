# MGX ARC GUI — handoff guide

Short guide for the **next maintainer** taking over this application inside NVIDIA.

---

## What this app does

**MGX ARC GUI** is a Flask web dashboard that proxies **Redfish** and **SSH** to MGX ARC **OpenBMC** controllers. It supports power control, sensors, firmware inventory/flash, I2C/USB/PCIe checks, and a configuration-driven **Fleet Health Console** (`/api/arc`).

Stack: Python 3.9+, Flask, vanilla JavaScript (no frontend build). Production runs under **gunicorn** + **systemd**.

---

## Current state

| Item | Value |
| ---- | ----- |
| **Source (transitional)** | https://github.com/riddhigadd/mgx-arc-spt-validation |
| **Target hosting** | NVIDIA GitLab group (org repo — **transfer pending**) |
| **Production target** | ITSS Linux VM + HTTPS + SSO (see below) |
| **Optional legacy lab URL** | http://10.110.33.21:4281/ (Spark — not required for new deploys) |
| **SSO in code** | **Not implemented** — document-only; see [docs/SSO.md](docs/SSO.md) |

**Original author:** Riddhi Gaddamwar  
**Maintainer:** TBD (NVIDIA Platform/Lab team)

Lab default BMC credentials and security notes: [SECURITY.md](SECURITY.md).

---

## Action checklist for new owner

1. **Request/transfer repo** to NVIDIA GitLab group; update remotes and clone URLs.
2. **Provision VM** via [ITSS VM selection](https://itss.nvidia.com/#/vm/selection).
3. **Register app SSO** in [ITSS Applications](https://itss.nvidia.com/#/applications/) per **KB0028542** (OIDC Auth Code + PKCE). Details: [docs/SSO.md](docs/SSO.md).
4. **Assign users/groups** in [Azure AD Enterprise applications](https://portal.azure.com/?feature.msaljs=true#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview/menuId~/null).
5. **Request HTTPS cert** via [ServiceNow](https://nvidia.service-now.com/esc?id=sc_cat_item&sys_id=855c03ad97efed50275ef7300153af0b).
6. **Deploy** following [DEPLOYMENT.md](DEPLOYMENT.md) (clone from org repo, venv, systemd, nginx).
7. **Validate internal access** per [KB0029440](https://nvidia.service-now.com/esc?id=kb_article&sysparm_article=KB0029440).
8. **Rotate/limit Spark credentials** if `deploy/push_to_spark.py` is still used.
9. **Implement OIDC middleware** (future PR) using env vars in [`.env.example`](.env.example).

Full list of non-automatable steps: [MANUAL_STEPS.md](MANUAL_STEPS.md).

---

## Key files

| Path | Purpose |
| ---- | ------- |
| `app.py` | Main Flask app, Direct Connect `/api/bmc` |
| `wsgi.py` | Gunicorn entry (`wsgi:app`) |
| `backend/` | Fleet Health API, config loader |
| `config/mgx_arc/` | `systems.yaml`, `profiles.yaml` |
| `deploy/mgx-arc-gui.service` | systemd unit template |
| `deploy/install_on_spark.sh` | First-time install script (works on any Linux systemd host) |
| `.env.example` | All documented environment variables |

---

## Contacts / ownership

| Role | Contact |
| ---- | ------- |
| **Maintainer** | Platform/Lab team TBD |
| **Original author** | Riddhi Gaddamwar |
| **ITSS / VM** | Via [ITSS portal](https://itss.nvidia.com/) |
| **SSO / app registration** | ITSS Applications + Azure AD owners for your org |

Update this table when ownership is assigned.

---

## Related documentation

- [README.md](README.md) — overview and quick start
- [DEPLOYMENT.md](DEPLOYMENT.md) — ITSS VM production deploy
- [docs/SSO.md](docs/SSO.md) — OIDC integration plan
- [MANUAL_STEPS.md](MANUAL_STEPS.md) — portal-only tasks
- [SECURITY.md](SECURITY.md) — secrets and lab defaults
- [deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md) — VPN/Tailscale (supplement to corp SSO/HTTPS)
