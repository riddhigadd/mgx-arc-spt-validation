# Manual steps (cannot be done in code alone)

These actions require NVIDIA internal portals, IT, or org admin access. Complete them during handoff and production deployment.

---

## Repository and ownership

- [ ] **Transfer repository** from personal GitHub ([riddhigadd/mgx-arc-spt-validation](https://github.com/riddhigadd/mgx-arc-spt-validation)) to an **NVIDIA GitLab group** (or approved org GitHub) and update clone URLs in docs/CI.
- [ ] Assign **maintainer** (Platform/Lab team TBD) with merge and deploy permissions.
- [ ] Rotate or revoke personal deploy keys / tokens used during development.

---

## Infrastructure (ITSS)

- [ ] **Provision Linux VM** via [ITSS VM selection](https://itss.nvidia.com/#/vm/selection) (Python 3.9+, network path to BMC lab subnet).
- [ ] Request **corporate DNS** A/ CNAME record for the VM hostname (e.g. `mgx-arc-gui.<site>.nvidia.com`).
- [ ] Open **firewall / network rules**: allow HTTPS (443) from intended user networks; allow outbound from VM to BMC management IPs (SSH 22, Redfish 443).
- [ ] Confirm **VPN / lab network access** for users who will use the GUI from off-site.

---

## Identity and access

- [ ] **Register application** in [ITSS Applications](https://itss.nvidia.com/#/applications/) — OIDC Authorization Code + PKCE per **KB0028542**.
- [ ] **Assign users/groups** in [Azure AD Enterprise applications](https://portal.azure.com/?feature.msaljs=true#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview/menuId~/null).
- [ ] **Request internal TLS certificate** via [ServiceNow certificate catalog item](https://nvidia.service-now.com/esc?id=sc_cat_item&sys_id=855c03ad97efed50275ef7300153af0b) for the public hostname.
- [ ] Install cert on nginx (or platform load balancer) and enforce HTTPS redirect.

---

## Secrets and lab credentials

- [ ] Create production `.env` on the VM from [`.env.example`](.env.example) — Fleet Health BMC/host credentials, future OIDC values.
- [ ] Review **lab default BMC credentials** (`root` / `0penBmc` in `app.py`) — change or restrict before wider exposure. See [SECURITY.md](SECURITY.md).
- [ ] Replace `TODO_MGX_ARC_*` placeholders in `config/mgx_arc/systems.yaml` with approved hostnames (secrets stay in env).
- [ ] **Rotate/limit Spark deploy credentials** if [deploy/push_to_spark.py](deploy/push_to_spark.py) remains in use (`SPARK_*` env vars).

---

## Post-deploy validation

- [ ] Deploy per [DEPLOYMENT.md](DEPLOYMENT.md) (gunicorn + systemd + nginx).
- [ ] Validate **internal browser access** per [KB0029440](https://nvidia.service-now.com/esc?id=kb_article&sysparm_article=KB0029440) (allowlist / policy issues).
- [ ] Smoke test: GUI load, BMC connect, one read-only Redfish call from the VM.

---

## Optional legacy lab host

- [ ] Decide fate of optional Spark instance `http://10.110.33.21:4281/` — decommission, migrate to ITSS VM, or keep as secondary with updated ownership.
