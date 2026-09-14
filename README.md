# MGX ARC GUI

A web dashboard for connecting to and managing **MGX ARC** systems. The app is a thin, safe proxy between your browser and each system's **OpenBMC** baseboard management controller (BMC), using **Redfish** over HTTPS and read-only or operational commands over SSH.

Built with **Flask** (Python) and **vanilla JavaScript** — no frontend build step required.

## Access the GUI (team use)

**Everyone uses the shared Spark-hosted instance:**

**http://10.110.33.21:4281/**

Open that URL in your browser, enter the BMC address and credentials, and connect. You do **not** need to install or run anything on your laptop for normal BMC work.

> **Do not** run a separate local copy for day-to-day lab use. GUI changes go through this Git repository (PR → merge → deploy to Spark). See [CONTRIBUTING.md](CONTRIBUTING.md) and [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md).

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

## How changes reach production

```
Contributor → GitHub PR → review & merge to main → maintainer deploys to Spark → team uses http://10.110.33.21:4281/
```

1. Fork, branch, and open a pull request ([CONTRIBUTING.md](CONTRIBUTING.md)).
2. After merge, a maintainer deploys the updated code to the Spark host ([deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)).
3. Users hard-refresh the browser (Ctrl+F5) to pick up front-end changes.

## Maintainer / developer deploy (Spark only)

Local `python app.py` on a laptop is **not** the supported workflow for team use. It exists only for maintainers who need to update the **shared Spark server**.

### First-time install on Spark

SSH to the DGX Spark (`10.110.33.21`), clone this repo, and run:

```bash
cd ~/MGXARC-GUI-RIDDHI   # or your clone path
bash deploy/install_on_spark.sh
```

This installs a systemd service (`mgx-arc-gui`) listening on `0.0.0.0:4281`.

Full procedure: **[deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)**

### Push updates after merge

From a maintainer machine with repo access and deploy credentials:

```powershell
$env:SPARK_HOST = "10.110.33.21"
$env:SPARK_USER = "sgaddamwar"
$env:SPARK_PASSWORD = "<from env or secret store>"
python deploy/push_to_spark.py
```

Or push specific files:

```powershell
python deploy/push_to_spark.py static/index.html static/js/app.js app.py
```

Credentials (`SPARK_USER`, `SPARK_PASSWORD`, etc.) are read from the environment only — never commit them. See `deploy/push_to_spark.py`.

### Local dev (maintainers only)

If you need to smoke-test before deploying, you may run locally on your machine — but **do not** share a localhost URL with the team:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

This listens on `http://localhost:4282/` by default. Use it only for pre-deploy validation, then push changes to Spark via the deploy scripts above.

## Configuration & environment variables

Configuration applies to the **Spark server**, not end-user laptops.

### Fleet Health Console (`config/mgx_arc/`)

For the configuration-driven **Fleet Health** mode, set credentials via environment variables on Spark (never in YAML):

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

## Remote access to Spark

If you cannot reach `10.110.33.21` directly, use NVIDIA VPN, Tailscale, or another approved path to the Spark host — still opening **http://10.110.33.21:4281/** (or the Spark's reachable IP on port 4281). See **[deploy/REMOTE_ACCESS.md](deploy/REMOTE_ACCESS.md)**.

## Customizing the GUI

Theme colors, tabs, API routes, I2C/USB maps, and fleet profiles are customized in this repo and redeployed to Spark. See **[CUSTOMIZATION.md](CUSTOMIZATION.md)**.

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
deploy/                   Spark install, deploy, and remote access docs
firmware_images/          Local firmware binaries (gitignored)
usb_golden_map.json       USB topology golden references
i2c_bus_map.json          I2C bus names and mux paths
i2c_expected_devices.json Expected I2C device addresses
```

## Firmware images

Place `.bin`, `.fwpkg`, and `.image` files in `firmware_images/` on the Spark host for one-click flashing. Large binaries are **gitignored** — see [firmware_images/README.txt](firmware_images/README.txt) for expected filenames.

Run `python discover_firmware.py <BMC_IP>` from the Spark (or a maintainer machine with BMC access) to probe a live BMC and map firmware inventory IDs.

## License

This project is released under the [MIT License](LICENSE).

## Disclaimer

**This is a lab / BMC management tool.** It is intended for use on trusted management networks with authorized hardware.

- **Do not** expose BMC management ports (SSH, Redfish) to the public internet.
- **Do not** run ad-hoc local GUI copies for team use — use the shared Spark instance.
- **Do not** commit real passwords, `.env` files, or deploy credentials to version control.

See [SECURITY.md](SECURITY.md) for responsible use and secret handling.
