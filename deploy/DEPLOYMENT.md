# Deploying MGX ARC GUI

> **Primary guide:** For NVIDIA ITSS VM production deployment (HTTPS, SSO planning, org repo), use **[DEPLOYMENT.md](../DEPLOYMENT.md)** at the repository root.

This document adds **supplementary** detail for running the GUI on **any Linux server** (lab VM, workstation, DGX, cloud instance, etc.) with a persistent systemd service.

| Item | Typical value |
| ---- | ------------- |
| **Listen port** | `4281` (production) or `4282` (dev) |
| **Service name** | `mgx-arc-gui` |
| **GitHub repo** | https://github.com/riddhigadd/mgx-arc-spt-validation |

---

## Architecture

```
[ Browsers on your network ]
        │
        │  lab LAN / VPN / Tailscale
        ▼
[ Your server — mgx-arc-gui :PORT ]
        │
        │  BMC management network
        ▼
[ MGX ARC BMCs ]
```

- The GUI proxies Redfish/SSH to BMCs from the server where it runs.
- The server must have network reachability to your BMC management IPs.
- BMC ports should never be exposed to the public internet.

---

## Quick start (development)

```bash
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git
cd mgx-arc-spt-validation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://localhost:4282/** (default dev port).

---

## Production install (systemd + gunicorn)

### Prerequisites

- Linux host with Python 3.9+
- Network reachability to BMC management IPs
- Sudo access for systemd install (optional but recommended)

### First-time install on the server

SSH to your server and clone the repo:

```bash
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git ~/mgx-arc-gui
cd ~/mgx-arc-gui
git checkout main
bash deploy/install_on_spark.sh
```

Custom paths / port:

```bash
APP_DIR=/opt/mgx-arc-gui PORT=4281 bash deploy/install_on_spark.sh
```

> **Note:** `install_on_spark.sh` was written for a lab DGX Spark but works on any Linux host with systemd.

The script will:

1. Rsync the project to `APP_DIR`
2. Create a Python venv and install `requirements.txt` + gunicorn
3. Install and enable the `mgx-arc-gui` systemd unit
4. Start the service on `0.0.0.0:$PORT`

### Manual systemd setup

If you prefer to configure systemd yourself:

```bash
cd /path/to/mgx-arc-gui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt gunicorn

# Test gunicorn manually
PORT=4281 gunicorn -w 2 -b 0.0.0.0:4281 app:app
```

Create `/etc/systemd/system/mgx-arc-gui.service`:

```ini
[Unit]
Description=MGX ARC GUI
After=network.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/mgx-arc-gui
Environment=PORT=4281
ExecStart=/path/to/mgx-arc-gui/.venv/bin/gunicorn -w 2 -b 0.0.0.0:4281 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable mgx-arc-gui
sudo systemctl start mgx-arc-gui
```

### Environment variables on the server

Set Fleet Health credentials and other secrets via systemd drop-in or `/etc/environment`:

| Variable | Purpose |
| -------- | ------- |
| `PORT` | Listen port (default `4281` in systemd install) |
| `MGX_ARC_BMC_USERNAME` | Fleet Health BMC username |
| `MGX_ARC_BMC_PASSWORD` | Fleet Health BMC password |
| `MGX_ARC_HOST_USERNAME` | OS host SSH username |
| `MGX_ARC_HOST_PASSWORD` | OS host SSH password |

---

## Routine update (after git pull)

On the server:

```bash
cd /path/to/mgx-arc-gui
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart mgx-arc-gui
```

### Remote push from a workstation (optional)

If you have SSH access to a remote server, use the included SFTP deploy helper:

```powershell
# PowerShell
$env:SPARK_HOST = "<your-server-ip>"
$env:SPARK_USER = "<ssh-user>"
$env:SPARK_PASSWORD = "<from secret store>"
python deploy/push_to_spark.py
```

```bash
# Bash
export SPARK_HOST=<your-server-ip>
export SPARK_USER=<ssh-user>
export SPARK_PASSWORD='<from secret store>'
python3 deploy/push_to_spark.py
```

Push specific files only:

```powershell
python deploy/push_to_spark.py static/index.html static/js/app.js app.py
```

### What `push_to_spark.py` does

1. SFTPs requested files to `SPARK_APP_DIR` (backs up replaced files under `deploy_backup/<timestamp>/`)
2. Re-runs `pip install -r requirements.txt` if `requirements.txt` was pushed
3. Runs `sudo systemctl restart mgx-arc-gui`
4. Checks `systemctl is-active mgx-arc-gui`

Default file set (when no arguments given) is listed in `DEFAULT_FILES` inside `push_to_spark.py`.

---

## Verify deployment

### On the server

```bash
systemctl is-active mgx-arc-gui          # expect: active
curl -I http://127.0.0.1:4281/           # expect: HTTP/1.x 200 or 302
sudo journalctl -u mgx-arc-gui -n 50 --no-pager
```

### From your laptop

```bash
curl -I http://<server-ip>:4281/
```

Open the URL in a browser, hard-refresh (Ctrl+F5), connect to a test BMC, and spot-check changed tabs.

---

## Service management

```bash
sudo systemctl status mgx-arc-gui
sudo systemctl restart mgx-arc-gui
sudo systemctl stop mgx-arc-gui
sudo journalctl -u mgx-arc-gui -f
```

---

## Firewall

Allow the GUI port only on trusted networks:

```bash
# Example with ufw — restrict to lab subnet
sudo ufw allow from 10.0.0.0/8 to any port 4281
sudo ufw enable
```

Do not expose port 4281 to the public internet without VPN, Tailscale, or a controlled tunnel. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md).

---

## Full reinstall vs incremental push

| Scenario | Command |
| -------- | ------- |
| New host or corrupted install | `bash deploy/install_on_spark.sh` on the server |
| Normal post-merge update | `git pull` + `systemctl restart`, or `python deploy/push_to_spark.py` |
| Dependency change | Include `requirements.txt` in push (default set does) |
| Fleet config only | `python deploy/push_to_spark.py config/mgx_arc/profiles.yaml` |

---

## Remote access

If the server is not directly reachable, use VPN, Tailscale, or another private path. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md).

---

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| Browser cannot reach `:4281` | VPN/Tailscale, firewall (`ufw`), server IP |
| Service inactive after restart | `journalctl -u mgx-arc-gui`; Python import errors in log |
| UI looks stale after deploy | Hard refresh (Ctrl+F5); confirm `index.html` / `app.js` were updated |
| BMC connect fails from GUI | Server can `ping`/SSH the BMC IP; GUI is a proxy |
| Permission denied on SFTP | `SPARK_APP_DIR` writable by SSH user |

---

## Security reminders

- Never commit SSH passwords or BMC credentials.
- Do not expose BMC SSH/Redfish to WAN.
- Restrict GUI port access to trusted networks.

See [SECURITY.md](../SECURITY.md).

---

## Optional example: DGX Spark lab host

One NVIDIA lab runs a shared instance for convenience:

| Item | Value |
| ---- | ----- |
| **Example URL** | http://10.110.33.21:4281/ |
| **Host** | `10.110.33.21` |
| **Default `SPARK_HOST`** | `10.110.33.21` in `push_to_spark.py` |

This is **one optional hosted copy**, not a requirement. You can deploy entirely on your own hardware using the same procedures above with your server's IP and credentials.
