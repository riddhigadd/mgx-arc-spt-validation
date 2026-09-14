# MGX ARC GUI

A web dashboard for connecting to and managing **MGX ARC** systems. The app is a thin, safe proxy between your browser and each system's **OpenBMC** baseboard management controller (BMC), using **Redfish** over HTTPS and read-only or operational commands over SSH.

Built with **Flask** (Python) and **vanilla JavaScript** — no frontend build step required.

## Quick start (local or any server)

Clone the repo, create a virtual environment, install dependencies, and run:

```powershell
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git
cd mgx-arc-spt-validation
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

```bash
git clone https://github.com/riddhigadd/mgx-arc-spt-validation.git
cd mgx-arc-spt-validation
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open **http://localhost:4282/** (default dev port), enter the BMC address and credentials, and connect.

For production on a server, use **gunicorn** and a process manager (systemd). See **[deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)**.

## Deploy anywhere

You can run this GUI on:

- Your **laptop** (development or personal use)
- A **lab VM** or workstation
- A **DGX Spark** or other server on your network
- Any **Linux host** that can reach your BMC management network

There is no requirement to use a shared or central host. Fork the repo, customize it, and deploy on the machine you choose.

> **Example hosted instance:** One NVIDIA lab runs a shared copy at **http://10.110.33.21:4281/** — that is an optional convenience, not a requirement.

## Creator

**Original author:** [Riddhi Gaddamwar](AUTHORS) — MGX ARC GUI for lab bring-up, firmware management, and system validation.

See [AUTHORS](AUTHORS) for attribution details.

## Features

- **BMC connect** — enter the BMC address and credentials at login; invalid logins are rejected server-side before any BMC access.
- **Overview** — live system summary (host name, model, BMC version, power state, kernel, uptime) via SSH.
- **Power control** — On, Graceful Shutdown, Force Off, Restart, Power Cycle (Redfish `ComputerSystem.Reset`).
- **Sensors** — temperatures, voltages, fans, and power draw (Redfish `Thermal` / `Power`).
- **Firmware** — MGX ARC firmware parts from BMC Redfish `FirmwareInventory` (`FW_BMC_0`, `FW_CPU_0`, `FW_ERoT_CPU_0`, `FW_FPGA_0`, `FW_CX8_*`, `FW_GPU_*`, `FW_HPM_SMA_*`).
- **Flash** — BMC, FML bundle, CX8, SMR/FPGA, SBIOS/UEFI, and ERoT. Place images in `firmware_images/` or upload from the GUI.
- **I2C checks** — bus discovery, mux paths, register dump/read/write over SSH.
- **USB / PCIe / KVM / Scope** — enumeration, inventory, console helpers, and optional lab-scope integration.
- **Fleet Health Console** — configuration-driven health, inventory, and recovery workflows (`/api/arc`).

## Contributing workflow

```
Contributor → GitHub PR → review & merge to main → deploy on your host → users open your URL
```

1. Fork, branch, and open a pull request ([CONTRIBUTING.md](CONTRIBUTING.md)).
2. Test locally with `python app.py` or deploy to your own server ([deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)).
3. After merge, deploy updated code wherever you run the GUI and hard-refresh the browser (Ctrl+F5) for front-end changes.

## Production deployment

For a persistent server install (systemd, gunicorn, firewall, `PORT` env var):

**[deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)**

### Environment variables

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `PORT` | `4282` (dev) / `4281` (systemd) | Listen port |
| `MGX_ARC_BMC_USERNAME` | — | Fleet Health BMC username |
| `MGX_ARC_BMC_PASSWORD` | — | Fleet Health BMC password |
| `MGX_ARC_HOST_USERNAME` | — | OS host SSH username |
| `MGX_ARC_HOST_PASSWORD` | — | OS host SSH password |
| `MGX_ARC_CONFIG_DIR` | `config/mgx_arc/` | Override config directory |
| `MGX_ARC_DATA_DIR` | — | Override snapshot/store data directory |

See [config/mgx_arc/README.md](config/mgx_arc/README.md) for `systems.yaml`, `profiles.yaml`, and placeholder (`TODO_MGX_ARC_*`) workflow.

### Direct Connect mode (legacy `/api/bmc` routes)

The Direct Connect login validates BMC credentials server-side. Default lab OpenBMC credentials (`root` / `0penBmc`) are baked into `app.py` for convenience — **change these for your environment** before exposing the GUI beyond a trusted lab network. See [SECURITY.md](SECURITY.md).

Other optional tuning variables include `FLASH_UPLOAD_TIMEOUT`, `BMC_UPDATE_TIMEOUT`, `I2C_CMD_TIMEOUT`, and `SCOPE_*` settings (documented in `app.py`).

## Remote access

If your GUI host is not directly reachable from your laptop, use VPN, Tailscale, or another private path to the server running the app. See **[deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md)**.

## Customizing the GUI

Theme colors, tabs, API routes, I2C/USB maps, and fleet profiles are customized in this repo. Test locally or on your server, then deploy wherever you run the GUI. See **[CUSTOMIZATION.md](CUSTOMIZATION.md)**.

## Contributing

Contributions are welcome. Please read **[CONTRIBUTING.md](CONTRIBUTING.md)** for fork/PR workflow, code style, and what not to commit.

## Project layout

```
app.py                    Flask backend + Redfish/SSH proxy
backend/                  Fleet Health API, config loader, providers
config/mgx_arc/           YAML systems & capability profiles
static/
  index.html              Dashboard shell
  css/style.css           Theme (CSS variables)
  js/app.js               Direct Connect tabs & API calls
  js/health-console.js    Fleet Health Console
deploy/                   Server install, deploy scripts, and remote access docs
firmware_images/          Local firmware binaries (gitignored)
usb_golden_map.json       USB topology golden references
i2c_bus_map.json          I2C bus names and mux paths
i2c_expected_devices.json Expected I2C device addresses
```

## Firmware images

Place `.bin`, `.fwpkg`, and `.image` files in `firmware_images/` on the host running the GUI for one-click flashing. Large binaries are **gitignored** — see [firmware_images/README.txt](firmware_images/README.txt) for expected filenames.

Run `python discover_firmware.py <BMC_IP>` from a machine with BMC network access to probe a live BMC and map firmware inventory IDs.

## License

This project is released under the [MIT License](LICENSE).

## Disclaimer

**This is a lab / BMC management tool.** It is intended for use on trusted management networks with authorized hardware.

- **Do not** expose BMC management ports (SSH, Redfish) to the public internet.
- **Do not** commit real passwords, `.env` files, or deploy credentials to version control.

See [SECURITY.md](SECURITY.md) for responsible use and secret handling.
