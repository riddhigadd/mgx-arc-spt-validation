# MGX ARC GUI on DGX Spark — access from anywhere

## Architecture (important)

```
[ Your laptop anywhere ]
        │
        │  NVIDIA VPN  or  Tailscale  or  Cloudflare Tunnel
        ▼
[ DGX Spark — runs this GUI on port 4281 ]
        │
        │  lab / management network only
        ▼
[ MGX ARC BMCs  e.g. 10.137.x.x ]
```

- The **GUI** must run on a machine that can already reach BMC IPs (Spark on the lab network).
- Your browser only talks to the **GUI**, never directly to BMCs from home.
- Do **not** put BMC management ports (SSH/Redfish) on the public internet.

---

## 1. Install on the DGX Spark

Copy this project to the Spark (USB, scp, git, OneDrive sync, etc.), then:

```bash
cd ~/MGXARC-GUI-RIDDHI   # or wherever you put the project
bash deploy/install_on_spark.sh
```

Or with a custom install path / port:

```bash
APP_DIR=/opt/mgx-arc-gui PORT=4281 bash deploy/install_on_spark.sh
```

Useful commands:

```bash
sudo systemctl status mgx-arc-gui
sudo systemctl restart mgx-arc-gui
sudo journalctl -u mgx-arc-gui -f
```

Confirm locally on the Spark:

```bash
curl -I http://127.0.0.1:4281/
```

---

## 2. Reach it from home / NVIDIA HQ / anywhere

### Option A — NVIDIA VPN (best if Spark is on NVIDIA network)

1. Connect your laptop to **NVIDIA VPN**.
2. Find the Spark’s corporate / lab IP (`hostname -I` on the Spark).
3. Open: `http://<spark-ip>:4281/`

Works from home and HQ as long as VPN is up and the Spark IP is reachable on the corp network.

### Option B — Tailscale (best “works from anywhere” private mesh)

On the **DGX Spark**:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

On your **laptop** (home, HQ, travel): install Tailscale, sign in with the same account/tailnet, then open:

`http://<spark-tailscale-ip>:4281/`

No public ports, no VPN split-tunnel fights, works from any Wi‑Fi.

### Option C — Cloudflare Tunnel (public HTTPS URL)

Use only if you intentionally want a shareable HTTPS link and accept that anyone with the URL can hit the login page.

```bash
# On the Spark (example)
cloudflared tunnel login
cloudflared tunnel create mgx-arc-gui
cloudflared tunnel route dns mgx-arc-gui mgx-arc-gui.example.com
cloudflared tunnel run --url http://127.0.0.1:4281 mgx-arc-gui
```

Then open `https://mgx-arc-gui.example.com`.

### Avoid

- Port-forwarding BMC networks to the public internet
- Exposing `:4281` on a public WAN IP without VPN/Tailscale/tunnel + access control

---

## 3. Checklist

| Check | Why |
| ----- | --- |
| Spark can `ping` / SSH the BMC IP | GUI is a proxy; it must reach BMCs |
| `systemctl is-active mgx-arc-gui` is `active` | Service stays up after reboot |
| Laptop can open Spark:4281 via VPN or Tailscale | Remote access path |
| Firewall allows 4281 only to trusted nets / Tailscale | Don’t leave it world-open |

---

## 4. Quick test from your laptop

```bash
# After VPN or Tailscale is connected:
curl -I http://<spark-ip>:4281/
```

Then open that URL in a browser, connect with the BMC IP (e.g. `10.137.156.84`), and use the GUI as usual.
