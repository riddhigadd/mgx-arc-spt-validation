# MGX ARC GUI

A web dashboard for connecting to and managing **MGX ARC** systems. The app is a thin, safe proxy between your browser and each system's **OpenBMC** baseboard management controller (BMC), using **Redfish** over HTTPS and read-only or operational commands over SSH.

Built with **Flask** (Python) and **vanilla JavaScript** — no frontend build step required.

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

## Prerequisites

- **Python 3.9+**
- **Network access** to each system's BMC management IP:
  - HTTPS (Redfish) — self-signed TLS is accepted on lab BMCs.
  - SSH (port 22) — overview, flash, I2C, and optional host commands.
- BMC image with `i2cdetect`, `i2cdump`, `i2cget`, and `i2cset` for the I2C tab.

## Local setup

```powershell
# From the project folder
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open **http://localhost:4282/** in your browser.

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

### Custom port

```powershell
$env:PORT = "8080"; python app.py
```

## Configuration & environment variables

### Fleet Health Console (`config/mgx_arc/`)

For the configuration-driven **Fleet Health** mode, set credentials via environment variables (never in YAML):

| Variable | Purpose |
| -------- | ------- |
| `MGX_ARC_BMC_USERNAME` | BMC SSH / Redfish username |
| `MGX_ARC_BMC_PASSWORD` | BMC password |
| `MGX_ARC_HOST_USERNAME` | OS host SSH username |
| `MGX_ARC_HOST_PASSWORD` | OS host SSH password |
| `MGX_ARC_CONFIG_DIR` | Override config directory (default: `config/mgx_arc/`) |
| `MGX_ARC_DATA_DIR` | Override snapshot/store data directory |

See [config/mgx_arc/README.md](config/mgx_arc/README.md) for `systems.yaml`, `profiles.yaml`, and placeholder (`TODO_MGX_ARC_*`) workflow.

### Direct Connect mode (legacy `/api/bmc` routes)

The Direct Connect login validates BMC credentials server-side. Default lab OpenBMC credentials (`root` / `0penBmc`) are baked into `app.py` for convenience — **change these for your environment** before exposing the GUI beyond a trusted lab network. See [SECURITY.md](SECURITY.md).

Other optional tuning variables include `FLASH_UPLOAD_TIMEOUT`, `BMC_UPDATE_TIMEOUT`, `I2C_CMD_TIMEOUT`, and `SCOPE_*` settings (documented in `app.py`).

## Deploy on DGX Spark

Run the GUI on a **DGX Spark** (or any Linux host) that can already reach BMC IPs on the lab network:

```bash
bash deploy/install_on_spark.sh
```

This installs a systemd service (`mgx-arc-gui`) listening on `0.0.0.0:4281`.

For remote access (VPN, Tailscale, Cloudflare Tunnel) and security guidance, see **[deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md)**.

Deploy credentials (`SPARK_USER`, `SPARK_PASSWORD`, etc.) are read from the environment only — see `deploy/push_to_spark.py`.

## Customizing the GUI

Theme colors, tabs, API routes, I2C/USB maps, and fleet profiles are all customizable without a build toolchain. See **[CUSTOMIZATION.md](CUSTOMIZATION.md)**.

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
deploy/                   Spark install & remote access docs
firmware_images/          Local firmware binaries (gitignored)
usb_golden_map.json       USB topology golden references
i2c_bus_map.json          I2C bus names and mux paths
i2c_expected_devices.json Expected I2C device addresses
```

## Firmware images

Place `.bin`, `.fwpkg`, and `.image` files in `firmware_images/` for one-click flashing. Large binaries are **gitignored** — see [firmware_images/README.txt](firmware_images/README.txt) for expected filenames.

Run `python discover_firmware.py <BMC_IP>` from the terminal to probe a live BMC and map firmware inventory IDs.

## License

This project is released under the [MIT License](LICENSE).

## Disclaimer

**This is a lab / BMC management tool.** It is intended for use on trusted management networks with authorized hardware.

- **Do not** expose BMC management ports (SSH, Redfish) to the public internet.
- **Do not** run this GUI on an untrusted WAN without VPN, Tailscale, or equivalent access control.
- **Do not** commit real passwords, `.env` files, or deploy credentials to version control.

See [SECURITY.md](SECURITY.md) for responsible use and secret handling.
