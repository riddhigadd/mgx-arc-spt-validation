# Remote access to MGX ARC GUI

How to reach the GUI when it runs on a server that is not directly on your local network.

Install and update procedures: **[DEPLOYMENT.md](DEPLOYMENT.md)**

---

## Architecture

```
[ Your laptop anywhere ]
        │
        │  VPN  or  Tailscale  or  lab LAN
        ▼
[ Your server — mgx-arc-gui on port PORT ]
        │
        │  lab / management network only
        ▼
[ MGX ARC BMCs  e.g. 10.x.x.x ]
```

- The **GUI** runs on a server that can reach BMC IPs on the management network.
- Your browser talks to **`<server>:PORT`**, not directly to BMCs from home.
- Do **not** put BMC management ports (SSH/Redfish) on the public internet.

Default ports: **4281** (production/systemd) or **4282** (`python app.py` dev mode).

---

## Reach your GUI host

Replace `<server>` and `<port>` with your deployment's IP/hostname and listen port.

### Option A — Same lab or corp network (simplest)

If your laptop is on the same reachable network as the server:

Open **http://\<server\>:\<port\>/**

### Option B — Corporate VPN

1. Connect your laptop to your organization's **VPN**.
2. Open **http://\<server\>:\<port\>/**.

Works from home and HQ as long as VPN is up and the server IP is reachable through the tunnel.

### Option C — Tailscale (private mesh)

On the **server**:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4
```

On your **laptop**: install Tailscale, sign in with the same account/tailnet, then open:

`http://<server-tailscale-ip>:<port>/`

No public ports, no VPN split-tunnel issues, works from any Wi‑Fi.

### Option D — Cloudflare Tunnel (public HTTPS URL)

Use only if you intentionally want a shareable HTTPS link and accept that anyone with the URL can hit the login page.

```bash
# On the server (example)
cloudflared tunnel login
cloudflared tunnel create mgx-arc-gui
cloudflared tunnel route dns mgx-arc-gui mgx-arc-gui.example.com
cloudflared tunnel run --url http://127.0.0.1:4281 mgx-arc-gui
```

Then open `https://mgx-arc-gui.example.com`.

### Avoid

- Port-forwarding BMC networks to the public internet
- Exposing the GUI port on a public WAN IP without VPN/Tailscale/tunnel + access control
- Leaving BMC SSH/Redfish reachable from the internet

---

## Service checks on the server

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
| Server can `ping` / SSH the BMC IP | GUI is a proxy; it must reach BMCs |
| `systemctl is-active mgx-arc-gui` is `active` | Service stays up after reboot |
| Laptop can open `<server>:<port>` via VPN, Tailscale, or lab LAN | Remote access path works |
| Firewall allows GUI port only to trusted nets / Tailscale | Don't leave it world-open |

---

## Quick test from your laptop

```bash
# After VPN or Tailscale is connected:
curl -I http://<server>:4281/
```

Then open that URL in a browser, connect with the BMC IP, and use the GUI as usual.

---

## Optional example: NVIDIA lab Spark host

One lab runs a shared instance at **http://10.110.33.21:4281/** for team convenience. The same remote-access options above apply — use VPN or Tailscale to reach `10.110.33.21` if you are not on the lab network. This is not required; deploy on your own host instead.
