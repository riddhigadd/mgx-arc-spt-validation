# Production deployment (NVIDIA ITSS Linux VM)

Guide for running MGX ARC GUI on an **internal NVIDIA Linux VM** provisioned through ITSS, with **HTTPS**, **SSO** (planned), and network access to the BMC lab.

For local development, see [README.md](README.md). For handoff ownership tasks, see [HANDOFF.md](HANDOFF.md).

| Item | Typical value |
| ---- | ------------- |
| **Listen port (app)** | `4281` (production) / `4282` (dev) |
| **Public URL** | `https://<hostname>.nvidia.com/` (nginx :443 → gunicorn) |
| **Service name** | `mgx-arc-gui` |
| **Repo (target)** | NVIDIA GitLab group — migrate from transitional GitHub |

---

## Architecture

```
[ Browsers — NVIDIA SSO / corp network ]
        │
        │  HTTPS :443 (nginx TLS termination)
        ▼
[ ITSS Linux VM — gunicorn mgx-arc-gui :4281 ]
        │
        │  BMC management network (SSH 22, Redfish 443)
        ▼
[ MGX ARC BMCs ]
```

- The GUI proxies Redfish/SSH from the VM; the VM must reach BMC management IPs.
- Do **not** expose BMC ports to the public internet.
- Browser SSO (OIDC) is documented in [docs/SSO.md](docs/SSO.md) — **implement in a follow-up PR**.

---

## VM sizing (ITSS)

Request a VM via [ITSS VM selection](https://itss.nvidia.com/#/vm/selection).

| Requirement | Suggestion |
| ----------- | ---------- |
| **OS** | Ubuntu 22.04 LTS or equivalent NVIDIA-standard Linux |
| **Python** | 3.9 or newer |
| **CPU / RAM** | 2 vCPU, 4 GB RAM minimum (firmware uploads are I/O bound; 4 GB comfortable) |
| **Disk** | 20 GB+ (venv, logs, optional `firmware_images/`) |
| **Network** | Route to BMC lab subnet; inbound 443 from user networks; outbound to BMC IPs |

Document the chosen hostname for DNS and TLS cert requests.

---

## First-time install

### 1. Clone from org repository

After GitLab transfer, clone from the **NVIDIA org remote** (example — replace with actual group URL):

```bash
sudo mkdir -p /opt/mgx-arc-gui
sudo chown "$USER":"$USER" /opt/mgx-arc-gui
git clone https://gitlab-master.nvidia.com/<group>/mgx-arc-gui.git /opt/mgx-arc-gui
cd /opt/mgx-arc-gui
git checkout main
```

Until transfer completes, the transitional GitHub URL may be used for bootstrap only — do not treat it as the long-term source of truth.

### 2. Environment file

```bash
cp .env.example .env
chmod 600 .env
# Edit .env — set MGX_ARC_* credentials, PORT, future OIDC_* values
```

See [`.env.example`](.env.example) for the full variable list.

### 3. Automated install (recommended)

```bash
cd /opt/mgx-arc-gui
APP_DIR=/opt/mgx-arc-gui PORT=4281 SERVICE_USER="$USER" bash deploy/install_on_spark.sh
```

The script rsyncs the tree, creates `.venv`, installs gunicorn, installs `deploy/mgx-arc-gui.service`, and starts systemd.

### 4. Load secrets via systemd (optional)

Create `/etc/systemd/system/mgx-arc-gui.service.d/env.conf`:

```ini
[Service]
EnvironmentFile=/opt/mgx-arc-gui/.env
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart mgx-arc-gui
```

### 5. Manual systemd reference

Template: [`deploy/mgx-arc-gui.service`](deploy/mgx-arc-gui.service)

```bash
cd /opt/mgx-arc-gui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt "gunicorn>=22.0.0"

# Quick test
PORT=4281 gunicorn -w 2 --threads 8 --timeout 900 -b 127.0.0.1:4281 wsgi:app
```

Production binds gunicorn to `127.0.0.1:4281` when nginx terminates TLS (adjust unit accordingly).

---

## HTTPS and reverse proxy (nginx)

1. Request an **internal TLS certificate** via [ServiceNow](https://nvidia.service-now.com/esc?id=sc_cat_item&sys_id=855c03ad97efed50275ef7300153af0b) for your VM hostname.
2. Install cert + key (paths vary by IT process).
3. Configure nginx for TLS termination and WebSocket support (scope VNC uses `/ws`):

```nginx
server {
    listen 443 ssl http2;
    server_name mgx-arc-gui.example.nvidia.com;

    ssl_certificate     /etc/ssl/certs/mgx-arc-gui.crt;
    ssl_certificate_key /etc/ssl/private/mgx-arc-gui.key;

    client_max_body_size 512m;   # firmware uploads

    location / {
        proxy_pass http://127.0.0.1:4281;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 900s;
        proxy_send_timeout 900s;
    }

    location /ws {
        proxy_pass http://127.0.0.1:4281;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400;
    }
}

server {
    listen 80;
    server_name mgx-arc-gui.example.nvidia.com;
    return 301 https://$host$request_uri;
}
```

Update `mgx-arc-gui.service` `ExecStart` bind to `127.0.0.1:4281` when nginx is in front.

Register the HTTPS callback URL in ITSS when enabling SSO: [docs/SSO.md](docs/SSO.md).

---

## Firewall

Restrict the app port to localhost if nginx handles external traffic:

```bash
# External users hit :443 only
sudo ufw allow 443/tcp

# If exposing gunicorn directly on lab LAN (not recommended with SSO):
# sudo ufw allow from 10.0.0.0/8 to any port 4281 proto tcp
```

Ensure outbound access from the VM to BMC management subnets.

---

## Ports summary

| Port | Use |
| ---- | --- |
| **4282** | Default `python app.py` development |
| **4281** | Production gunicorn (systemd default) |
| **443** | nginx HTTPS (user-facing) |
| **22 / 443** | Outbound from VM to BMC SSH / Redfish |

---

## Routine updates

```bash
cd /opt/mgx-arc-gui
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart mgx-arc-gui
```

### Optional: remote push from a workstation

Legacy lab helper — not required for ITSS VM operation. See [`deploy/push_to_spark.py`](deploy/push_to_spark.py) and `SPARK_*` vars in [`.env.example`](.env.example).

---

## Post-deploy validation

### On the VM

```bash
systemctl is-active mgx-arc-gui
curl -I http://127.0.0.1:4281/
sudo journalctl -u mgx-arc-gui -n 50 --no-pager
curl -Ik https://mgx-arc-gui.example.nvidia.com/
```

### Internal browser access

Follow NVIDIA guidance for internal web apps: [KB0029440](https://nvidia.service-now.com/esc?id=kb_article&sysparm_article=KB0029440) (allowlist, corporate browser policies).

Smoke test: open the GUI, connect to an authorized BMC, verify overview/sensors.

---

## Service management

```bash
sudo systemctl status mgx-arc-gui
sudo systemctl restart mgx-arc-gui
sudo journalctl -u mgx-arc-gui -f
```

---

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| 502 from nginx | gunicorn running; bind address `127.0.0.1:4281` |
| SSO redirect errors | Redirect URI matches ITSS registration exactly |
| Fleet Health errors | `MGX_ARC_*` env vars set; `systems.yaml` placeholders replaced |
| BMC connect fails | VM can reach BMC IP; credentials; not a browser SSO issue |
| Upload timeout | nginx `proxy_read_timeout`; gunicorn `--timeout 900` |

---

## Security

- Secrets only in `.env` (gitignored) — see [SECURITY.md](SECURITY.md).
- Change lab defaults in `app.py` before wide deployment.
- SSO: [docs/SSO.md](docs/SSO.md).

---

## Optional legacy: DGX Spark lab host

One lab runs a shared instance at **http://10.110.33.21:4281/** for convenience. This is **optional**; new deployments should use ITSS VM + HTTPS. Spark deploy remains available via `deploy/push_to_spark.py`.

---

## See also

- [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) — extended notes (remote push, Spark-specific history)
- [deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md) — VPN/Tailscale supplement
- [HANDOFF.md](HANDOFF.md) — owner checklist
