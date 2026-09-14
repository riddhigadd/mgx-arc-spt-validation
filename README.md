# MGX ARC GUI

A web dashboard for connecting to and managing **MGX ARC** systems. The app is a thin, safe proxy between your browser and each system's **OpenBMC** baseboard management controller (BMC), using **Redfish** over HTTPS and read-only or operational commands over SSH.

Built with **Flask** (Python) and **vanilla JavaScript** — no frontend build step required.

**Original author:** [Riddhi Gaddamwar](AUTHORS)  
**Maintainer:** TBD (NVIDIA Platform/Lab team)

---

## Handoff and production deployment

This repository is prepared for **internal NVIDIA ownership** on an **ITSS Linux VM** with HTTPS and SSO (OIDC PKCE). The source currently lives on transitional personal GitHub and should move to an **NVIDIA GitLab group**.

| Document | Purpose |
| -------- | ------- |
| **[HANDOFF.md](HANDOFF.md)** | Checklist for the next maintainer |
| **[DEPLOYMENT.md](DEPLOYMENT.md)** | ITSS VM install, systemd, nginx, HTTPS |
| **[docs/SSO.md](docs/SSO.md)** | Azure AD / ITSS OIDC registration (integration TODO in code) |
| **[MANUAL_STEPS.md](MANUAL_STEPS.md)** | Portal-only tasks (VM, cert, repo transfer) |
| **[SECURITY.md](SECURITY.md)** | Secrets, lab defaults, network exposure |

**Who should own this:** Platform/Lab team TBD — see [HANDOFF.md](HANDOFF.md) for the full action checklist.

**Repository transfer:** Clone/bootstrap may use [github.com/riddhigadd/mgx-arc-spt-validation](https://github.com/riddhigadd/mgx-arc-spt-validation) until the org GitLab remote is ready; long-term maintainership must not depend on a personal account.

---

## Quick start (developers)

Local development is secondary to production deploy. For a laptop or lab workstation:

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

Copy [`.env.example`](.env.example) to `.env` for Fleet Health credentials and future SSO settings.

For production on an ITSS VM, follow **[DEPLOYMENT.md](DEPLOYMENT.md)** (gunicorn + systemd + nginx).

> **Optional legacy lab instance:** http://10.110.33.21:4281/ — not required for new deployments.

---

## Features

- **BMC connect** — enter the BMC address and credentials at login; invalid logins are rejected server-side before any BMC access.
- **Overview** — live system summary (host name, model, BMC version, power state, kernel, uptime) via SSH.
- **Power control** — On, Graceful Shutdown, Force Off, Restart, Power Cycle (Redfish `ComputerSystem.Reset`).
- **Sensors** — temperatures, voltages, fans, and power draw (Redfish `Thermal` / `Power`).
- **Firmware** — MGX ARC firmware parts from BMC Redfish `FirmwareInventory`.
- **Flash** — BMC, FML bundle, CX8, SMR/FPGA, SBIOS/UEFI, and ERoT. Place images in `firmware_images/` or upload from the GUI.
- **I2C checks** — bus discovery, mux paths, register dump/read/write over SSH.
- **USB / PCIe / KVM / Scope** — enumeration, inventory, console helpers, and optional lab-scope integration.
- **Fleet Health Console** — configuration-driven health, inventory, and recovery workflows (`/api/arc`).

---

## Environment variables

Copy [`.env.example`](.env.example) to `.env` (gitignored). Key variables:

| Variable | Purpose |
| -------- | ------- |
| `PORT` | Listen port (`4282` dev, `4281` production) |
| `MGX_ARC_BMC_USERNAME` / `MGX_ARC_BMC_PASSWORD` | Fleet Health BMC credentials |
| `MGX_ARC_HOST_USERNAME` / `MGX_ARC_HOST_PASSWORD` | OS host SSH credentials |
| `MGX_ARC_CONFIG_DIR` | Override config directory (default `config/mgx_arc/`) |
| `MGX_ARC_DATA_DIR` | Override snapshot/store data directory |
| `OIDC_*` | Planned SSO — see [docs/SSO.md](docs/SSO.md) |

Direct Connect login uses hardcoded lab defaults in `app.py` (`root` / `0penBmc`) — change before exposing beyond a trusted lab. See [SECURITY.md](SECURITY.md).

Optional tuning: `FLASH_UPLOAD_TIMEOUT`, `BMC_UPDATE_TIMEOUT`, `I2C_CMD_TIMEOUT`, `SCOPE_*` (documented in `.env.example`).

See [config/mgx_arc/README.md](config/mgx_arc/README.md) for `systems.yaml`, `profiles.yaml`, and `TODO_MGX_ARC_*` placeholders.

---

## Contributing

```
Contributor → PR → review & merge → deploy per DEPLOYMENT.md
```

Read **[CONTRIBUTING.md](CONTRIBUTING.md)** for fork/PR workflow and what not to commit.

---

## Project layout

```
app.py                    Flask backend + Redfish/SSH proxy
backend/                  Fleet Health API, config loader, providers
config/mgx_arc/           YAML systems & capability profiles
static/                   Dashboard UI (HTML, CSS, JS)
deploy/                   systemd unit, install script, remote push helper
docs/SSO.md               OIDC integration notes
firmware_images/          Local firmware binaries (gitignored)
```

---

## Firmware images

Place `.bin`, `.fwpkg`, and `.image` files in `firmware_images/` on the host for one-click flashing. Large binaries are **gitignored** — see [firmware_images/README.txt](firmware_images/README.txt).

Run `python discover_firmware.py <BMC_IP>` from a machine with BMC network access to map firmware inventory IDs.

---

## License

[MIT License](LICENSE)

---

## Disclaimer

**Lab / BMC management tool** for trusted management networks and authorized hardware.

- Do not expose BMC management ports to the public internet.
- Do not commit real passwords, `.env` files, or deploy credentials.

See [SECURITY.md](SECURITY.md).
