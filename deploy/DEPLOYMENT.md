# Deploying MGX ARC GUI to Spark

Canonical procedure for installing and updating the **shared team instance** on DGX Spark.

| Item | Value |
| ---- | ----- |
| **Production URL** | http://10.110.33.21:4281/ |
| **Spark host** | `10.110.33.21` |
| **Service name** | `mgx-arc-gui` |
| **Listen port** | `4281` |
| **GitHub repo** | https://github.com/riddhigadd/mgx-arc-spt-validation |

The team always uses the production URL above. Do not ask users to run localhost copies.

---

## Architecture

```
[ Team browsers ]
        │
        │  lab network / VPN / Tailscale
        ▼
[ DGX Spark @ 10.110.33.21 — mgx-arc-gui :4281 ]
        │
        │  BMC management network
        ▼
[ MGX ARC BMCs ]
```

- One shared GUI instance on Spark proxies Redfish/SSH to BMCs.
- Code changes flow: **Git PR → merge → deploy to Spark → users refresh browser**.
- BMC ports are never exposed to the public internet.

---

## Prerequisites

### On Spark (first install)

- Ubuntu / DGX OS with Python 3.9+
- Network reachability to BMC management IPs
- Sudo access for systemd install

### On maintainer machine (push updates)

- Clone of this repo at the merged commit
- SSH credentials via environment variables (never commit):

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `SPARK_HOST` | `10.110.33.21` | Target Spark IP |
| `SPARK_USER` | *(required)* | SSH username (e.g. `sgaddamwar`) |
| `SPARK_PASSWORD` | *(required)* | SSH password |
| `SPARK_APP_DIR` | `/home/<user>/MGXARC-GUI-RIDDHI` | Install path on Spark |

---

## First-time install

SSH to Spark and clone the repo (if not already present):

```bash
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git ~/MGXARC-GUI-RIDDHI
cd ~/MGXARC-GUI-RIDDHI
git checkout main
bash deploy/install_on_spark.sh
```

Custom paths / port:

```bash
APP_DIR=/opt/mgx-arc-gui PORT=4281 bash deploy/install_on_spark.sh
```

`install_on_spark.sh` will:

1. Rsync the project to `APP_DIR`
2. Create a Python venv and install `requirements.txt` + gunicorn
3. Install and enable the `mgx-arc-gui` systemd unit
4. Start the service on `0.0.0.0:4281`

---

## Routine update (after PR merge)

From your maintainer workstation, with latest `main` checked out:

```powershell
# PowerShell
$env:SPARK_HOST = "10.110.33.21"
$env:SPARK_USER = "sgaddamwar"
$env:SPARK_PASSWORD = "<from secret store>"
python deploy/push_to_spark.py
```

```bash
# Bash
export SPARK_HOST=10.110.33.21
export SPARK_USER=sgaddamwar
export SPARK_PASSWORD='<from secret store>'
python3 deploy/push_to_spark.py
```

### Push specific files only

```powershell
python deploy/push_to_spark.py static/index.html static/js/app.js app.py
```

### What `push_to_spark.py` does

1. SFTPs requested files to `SPARK_APP_DIR` (backs up replaced files under `deploy_backup/<timestamp>/`)
2. Re-runs `pip install -r requirements.txt` if `requirements.txt` was pushed
3. Runs `sudo systemctl restart mgx-arc-gui`
4. Checks `systemctl is-active mgx-arc-gui`

Default file set (when no arguments given) is listed in `DEFAULT_FILES` inside `push_to_spark.py` — includes `app.py`, front-end assets, backend config, and JSON maps.

---

## Verify deployment

### On Spark

```bash
systemctl is-active mgx-arc-gui          # expect: active
curl -I http://127.0.0.1:4281/           # expect: HTTP/1.x 200 or 302
sudo journalctl -u mgx-arc-gui -n 50 --no-pager
```

### From your laptop

```bash
curl -I http://10.110.33.21:4281/
```

Open **http://10.110.33.21:4281/** in a browser, hard-refresh (Ctrl+F5), connect to a test BMC, and spot-check changed tabs.

---

## Service management (on Spark)

```bash
sudo systemctl status mgx-arc-gui
sudo systemctl restart mgx-arc-gui
sudo systemctl stop mgx-arc-gui
sudo journalctl -u mgx-arc-gui -f
```

---

## Full reinstall vs incremental push

| Scenario | Command |
| -------- | ------- |
| New Spark host or corrupted install | `bash deploy/install_on_spark.sh` on Spark |
| Normal post-merge update | `python deploy/push_to_spark.py` from maintainer machine |
| Dependency change | Include `requirements.txt` in push (default set does) |
| Fleet config only | `python deploy/push_to_spark.py config/mgx_arc/profiles.yaml` |

---

## Remote access

If `10.110.33.21` is not directly reachable, use NVIDIA VPN, Tailscale, or another approved path — still targeting port **4281** on the Spark host. See [REMOTE_ACCESS.md](REMOTE_ACCESS.md).

---

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| Browser cannot reach `:4281` | VPN/Tailscale, firewall (`ufw`), Spark IP |
| Service inactive after push | `journalctl -u mgx-arc-gui`; Python import errors in log |
| UI looks stale after deploy | Hard refresh (Ctrl+F5); confirm `index.html` / `app.js` were pushed |
| BMC connect fails from GUI | Spark can `ping`/SSH the BMC IP; GUI is a proxy |
| Permission denied on SFTP | `SPARK_APP_DIR` writable by `SPARK_USER` |

---

## Security reminders

- Never commit `SPARK_PASSWORD` or BMC credentials.
- Do not expose BMC SSH/Redfish to WAN.
- The Spark GUI (`:4281`) is the **only** supported entry point for the team.

See [SECURITY.md](../SECURITY.md).
