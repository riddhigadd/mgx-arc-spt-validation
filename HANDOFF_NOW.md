# Handoff checklist — do today (2026-09-14)

Actionable steps for the **next maintainer** taking over MGX ARC GUI. Split into browser-only (you) vs already in repo.

**Repo (transitional):** https://github.com/riddhigadd/mgx-arc-spt-validation  
**Target:** NVIDIA GitLab + ITSS Linux VM + HTTPS + SSO

---

## Part A — You do in browser (~15 min)

Complete in order. **Copy values into a scratch file** — you need them for `.env` and nginx.

### 1. GitLab project (5 min)

| Step | Action |
| ---- | ------ |
| 1 | Open [NVIDIA GitLab](https://gitlab-master.nvidia.com/) |
| 2 | **New project** → **Create blank project** |
| 3 | Name: `mgx-arc-gui` · Visibility: Internal/group-private · **Do not** init README |
| 4 | **Copy HTTPS clone URL** → save as `GITLAB_URL` |

Paste-ready push (after URL): see [docs/GITLAB_MIGRATION.md](docs/GITLAB_MIGRATION.md).

---

### 2. ITSS Linux VM (5 min)

| Step | Action |
| ---- | ------ |
| 1 | Open [ITSS VM selection](https://itss.nvidia.com/#/vm/selection) |
| 2 | Request **Ubuntu 22.04 LTS** (or org-standard Linux), **2 vCPU / 4 GB RAM / 20 GB disk** |
| 3 | Network: route to **BMC lab subnet**; inbound **443** from user networks |
| 4 | Submit ticket / follow ITSS workflow for hostname |

**Copy back when provisioned:**

| Field | Example | Your value |
| ----- | ------- | ---------- |
| VM IP | `10.x.x.x` | __________ |
| Hostname (FQDN) | `mgx-arc-gui.<site>.nvidia.com` | __________ |
| SSH user | your LDAP / service account | __________ |

Use hostname everywhere below (DNS, cert, SSO redirect).

---

### 3. ITSS SSO app registration (5 min)

| Step | Action |
| ---- | ------ |
| 1 | Open [ITSS Applications](https://itss.nvidia.com/#/applications/) |
| 2 | **New application** (or request per **KB0028542**) |
| 3 | Protocol: **OIDC** · Flow: **Authorization Code + PKCE** |
| 4 | **Redirect URI** (exact, HTTPS, no trailing slash on path): |

```
https://<YOUR-HOSTNAME>/oauth2/callback
```

| Form field | What to enter |
| ---------- | ------------- |
| App name | `MGX ARC GUI` (or team standard) |
| Redirect URI | `https://<hostname>/oauth2/callback` |
| Scopes | `openid`, `profile`, `email` |
| Client type | Public (PKCE) unless ITSS requires confidential |

**Copy back from ITSS / Azure:**

| Field | Your value |
| ----- | ---------- |
| **Client ID** | __________ |
| **Tenant ID** | __________ |
| **Redirect URI** (confirmed) | __________ |

Reference: [docs/SSO.md](docs/SSO.md)

---

### 4. Azure AD — assign users/groups (2 min)

| Step | Action |
| ---- | ------ |
| 1 | Open [Azure AD Enterprise applications](https://portal.azure.com/?feature.msaljs=true#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview/menuId~/null) |
| 2 | Find the app registered via ITSS |
| 3 | **Users and groups** → add lab/platform security group(s) |

---

### 5. ServiceNow — internal TLS certificate (5 min)

| Step | Action |
| ---- | ------ |
| 1 | Open [ServiceNow certificate request](https://nvidia.service-now.com/esc?id=sc_cat_item&sys_id=855c03ad97efed50275ef7300153af0b) |
| 2 | **Common name / SAN:** `<YOUR-HOSTNAME>` (same FQDN as SSO redirect) |
| 3 | Submit; note ticket number |

**Copy back when issued:**

| Field | Typical path | Your value |
| ----- | ------------ | ---------- |
| Certificate file | `/etc/ssl/certs/mgx-arc-gui.crt` | __________ |
| Private key file | `/etc/ssl/private/mgx-arc-gui.key` | __________ |

---

### 6. Post-browser validation (internal policy)

After deploy: [KB0029440 — internal browser access](https://nvidia.service-now.com/esc?id=kb_article&sysparm_article=KB0029440)

---

## Part B — Already done in repo

| Item | Location |
| ---- | -------- |
| OIDC scaffold (off by default) | `backend/sso.py`, wired in `app.py` |
| Env template with `SSO_ENABLED` | `.env.example` |
| VM install script | `deploy/install_on_vm.sh` |
| nginx HTTPS example | `deploy/nginx-mgx-arc.conf.example` |
| GitLab migration commands | `docs/GITLAB_MIGRATION.md` |
| Handoff / deploy / SSO docs | `HANDOFF.md`, `DEPLOYMENT.md`, `docs/SSO.md` |

**Enable SSO on VM** (only after step 3 values exist):

```bash
# In /opt/mgx-arc-gui/.env
SSO_ENABLED=true
SECRET_KEY=<openssl rand -hex 32>
OIDC_CLIENT_ID=<from ITSS>
OIDC_TENANT_ID=<from ITSS>
OIDC_REDIRECT_URI=https://<hostname>/oauth2/callback
OIDC_SCOPES=openid profile email
sudo systemctl restart mgx-arc-gui
```

---

## Part C — After VM + credentials: deploy commands

SSH to the VM, then:

```bash
# 1. Clone (GitLab URL when ready; GitHub OK for bootstrap)
sudo mkdir -p /opt/mgx-arc-gui
sudo chown "$USER":"$USER" /opt/mgx-arc-gui
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git /opt/mgx-arc-gui
cd /opt/mgx-arc-gui
git pull origin main

# 2. Secrets
cp .env.example .env
chmod 600 .env
# Edit .env: MGX_ARC_BMC_*, MGX_ARC_HOST_*, SSO_* when ready

# 3. Install app (gunicorn on 127.0.0.1:4281)
APP_DIR=/opt/mgx-arc-gui PORT=4281 SERVICE_USER="$USER" bash deploy/install_on_vm.sh

# 4. Smoke test (before nginx)
curl -s http://127.0.0.1:4281/healthz

# 5. nginx + TLS (after cert issued)
sudo apt-get install -y nginx   # if needed
sudo cp deploy/nginx-mgx-arc.conf.example /etc/nginx/sites-available/mgx-arc-gui
# Edit server_name and ssl_certificate paths
sudo ln -sf /etc/nginx/sites-available/mgx-arc-gui /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 6. HTTPS smoke test
curl -Ik https://<hostname>/
```

Full guide: [DEPLOYMENT.md](DEPLOYMENT.md)

---

## Scratch template (fill as you go)

```
GITLAB_URL=
VM_IP=
VM_HOSTNAME=
VM_SSH_USER=
OIDC_CLIENT_ID=
OIDC_TENANT_ID=
OIDC_REDIRECT_URI=https://<hostname>/oauth2/callback
TLS_CERT_PATH=
TLS_KEY_PATH=
SERVICENOW_CERT_TICKET=
```

---

## Optional: legacy Spark lab

Shared instance: http://10.110.33.21:4281/ — decide keep/migrate/decommission. Not required for ITSS path.
