# Remote access to the shared MGX ARC GUI

## Official team URL

**http://10.110.33.21:4281/**

Everyone uses this Spark-hosted instance. Do not run or share localhost copies for team use.

Install and update procedures: **[DEPLOYMENT.md](DEPLOYMENT.md)**

---

## Architecture

```
[ Your laptop anywhere ]
        │
        │  NVIDIA VPN  or  Tailscale  or  lab LAN
        ▼
[ DGX Spark @ 10.110.33.21 — mgx-arc-gui on port 4281 ]
        │
        │  lab / management network only
        ▼
[ MGX ARC BMCs  e.g. 10.137.x.x ]
```

- The **GUI** runs on Spark, which can reach BMC IPs on the lab network.
- Your browser talks to **Spark:4281**, never directly to BMCs from home.
- Do **not** put BMC management ports (SSH/Redfish) on the public internet.

---

## Reach Spark from home / NVIDIA HQ / anywhere

### Option A — Lab or corp network (simplest)

If your laptop is on the same reachable network as `10.110.33.21`:

Open **http://10.110.33.21:4281/**

### Option B — NVIDIA VPN

1. Connect your laptop to **NVIDIA VPN**.
2. Open **http://10.110.33.21:4281/** (or the Spark's corp IP on port 4281 if different).

Works from home and HQ as long as VPN is up and the Spark IP is reachable.

### Option C — Tailscale (private mesh)

On the **DGX Spark**:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

On your **laptop**: install Tailscale, sign in with the same account/tailnet, then open:

`http://<spark-tailscale-ip>:4281/`

No public ports, no VPN split-tunnel fights, works from any Wi‑Fi.

### Option D — Cloudflare Tunnel (public HTTPS URL)

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
- Running `python app.py` on laptops and sharing localhost URLs with the team

---

## Maintainer: service checks on Spark

```bash
sudo systemctl status mgx-arc-gui
sudo systemctl restart mgx-arc-gui
sudo journalctl -u mgx-arc-gui -f
curl -I http://127.0.0.1:4281/
```

Full deploy/update workflow: **[DEPLOYMENT.md](DEPLOYMENT.md)**

---

## Checklist

| Check | Why |
| ----- | --- |
| Spark can `ping` / SSH the BMC IP | GUI is a proxy; it must reach BMCs |
| `systemctl is-active mgx-arc-gui` is `active` | Service stays up after reboot |
| Laptop can open Spark:4281 via VPN, Tailscale, or lab LAN | Remote access path |
| Firewall allows 4281 only to trusted nets / Tailscale | Don't leave it world-open |
| Team bookmark is http://10.110.33.21:4281/ | Single shared instance |

---

## Quick test from your laptop

```bash
# After VPN or Tailscale is connected:
curl -I http://10.110.33.21:4281/
```

Then open that URL in a browser, connect with the BMC IP (e.g. `10.137.156.84`), and use the GUI as usual.
