# Customizing the MGX ARC GUI

This guide covers practical ways to change the look, tabs, API routes, and lab configuration without adding a frontend build step.

## Deployment model

All customizations happen **in this Git repository** and are deployed to the **shared Spark instance**:

**http://10.110.33.21:4281/**

Workflow:

1. Edit files locally (or in a fork).
2. Open a pull request and get it merged to `main`.
3. A maintainer deploys to Spark with `deploy/push_to_spark.py` or `deploy/install_on_spark.sh` ([deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)).
4. The team hard-refreshes the browser to pick up changes.

**Do not** run a separate local GUI instance for team use. One shared Spark deployment is the official product.

## Theme & styling (`static/css/style.css`)

All visual tokens are CSS custom properties on `:root`:

```css
:root {
  --bg: #050705;
  --panel: #0d120e;
  --border: #1f2c22;
  --accent: #76b900;       /* primary accent (NVIDIA green) */
  --accent-bright: #8fe600;
  --danger: #ff5a3c;
  --warn: #ffc231;
  --radius: 12px;
  --font-display: "Orbitron", "Rajdhani", sans-serif;
  --font-body: "Inter", "Segoe UI", Roboto, sans-serif;
  --font-mono: "JetBrains Mono", Consolas, monospace;
}
```

**Quick theme changes:**

1. Edit `--accent`, `--accent-bright`, and `--accent-dim` for a new brand color.
2. Edit `--bg`, `--panel`, and `--text` for light/dark variants.
3. Change `--font-*` and the Google Fonts link in `static/index.html` for typography.
4. Adjust the watermark in `body::before` (NVIDIA eye SVG) or remove it.

Fleet Health Console styles are separate in `static/css/health-console.css`.

After CSS changes, deploy the updated files to Spark:

```powershell
python deploy/push_to_spark.py static/css/style.css static/css/health-console.css
```

## Page structure (`static/index.html`)

The shell has two modes toggled from the top bar:

| Section | ID | Purpose |
| ------- | -- | ------- |
| Login | `#login-view` | BMC IP + credentials |
| Direct Connect dashboard | `#dashboard-view` | Tabbed BMC console |
| Fleet Health | `#health-console-view` | Config-driven fleet views |

Direct Connect tabs are declared in `<nav id="tabs">`:

```html
<button class="tab active" data-tab="overview">Overview</button>
<button class="tab" data-tab="power">Power</button>
<!-- ... -->
```

Each tab needs a matching panel:

```html
<div id="panel-overview" class="tab-panel"></div>
<div id="panel-power" class="tab-panel hidden"></div>
```

Front-end changes (`index.html`, `app.js`, CSS) must be deployed together — see `DEFAULT_FILES` in `deploy/push_to_spark.py`.

## Tabs & loaders (`static/js/app.js`)

Tab switching is centralized in `switchTab()`:

```javascript
const loaders = {
  overview: loadOverview,
  power: loadPower,
  firmware: loadFirmware,
  flash: loadFlash,
  shell: loadBmcShell,
  spi: loadSpiRead,
  i2c: loadI2c,
  usb: loadUsbEnum,
  pcie: loadPcie,
  kvm: loadKvm,
  scope: loadScope,
};
loaders[name](panel);
```

**To add a new Direct Connect tab:**

1. Add a `<button class="tab" data-tab="mytab">My Tab</button>` in `index.html`.
2. Add `<div id="panel-mytab" class="tab-panel hidden"></div>`.
3. Implement `async function loadMyTab(panel) { ... }` in `app.js`.
4. Register `mytab: loadMyTab` in the `loaders` object.
5. Deploy `static/index.html` and `static/js/app.js` to Spark.

Loaders typically call `apiFetch("/api/bmc/...")` and render into `panel` using `el()` helpers. Use `withLoader(panel, fn)` for the standard loading spinner (NVIDIA eye animation).

Shared state lives in the `state` object at the top of `app.js` (`bmcIp`, `creds`, `activeTab`, etc.).

### Fleet Health tabs (`static/js/health-console.js`)

Health Console tabs use `data-health-tab` attributes and `#health-panel-*` divs. Extend `health-console.js` following the same pattern as existing MCTP, Inventory, and Recovery views. Backend routes are under `/api/arc/*` in `backend/api.py`.

## Adding API routes (`app.py`)

Direct Connect routes use the `/api/bmc` prefix and share BMC auth via request headers:

- `X-BMC-IP` — target BMC address
- `X-BMC-User` / `X-BMC-Pass` — credentials (validated by `_validate_bmc_credentials`)

Example skeleton:

```python
@app.route("/api/bmc/my-feature", methods=["GET"])
def api_my_feature():
    bmc_ip, username, password, err = _require_bmc_session()
    if err:
        return err
    # ... call Redfish or SSH via _ssh_connect / _redfish_get ...
    return jsonify({"ok": True, "data": result})
```

Register the route near related endpoints in `app.py`. Reuse `_ssh_connect`, `_redfish_get`, and `_require_bmc_session` rather than duplicating auth logic.

For fleet/config-driven features, add routes to `backend/api.py` (Blueprint `/api/arc`) and implement logic in `backend/services/`.

After backend changes, deploy `app.py` and any touched `backend/` files, then restart the service (handled automatically by `push_to_spark.py`).

## USB golden map (`usb_golden_map.json`)

Defines expected USB topology for BMC and OS views used by USB enumeration and Fleet Health comparisons.

Structure:

- Top-level `views` with `bmc` and `os` keys.
- Each view has a tree of nodes (`id`, `name`, `vid`, `pid`, `children`).
- Used when a profile does not explicitly set `usb.golden_map` in YAML.

Edit this file to match your board's golden USB inventory. Do not put credentials here. Deploy with:

```powershell
python deploy/push_to_spark.py usb_golden_map.json
```

## I2C configuration

Two JSON files supplement the I2C tab (also loaded into YAML profiles when not overridden):

### `i2c_bus_map.json`

- `buses` — map bus numbers to human names, subsystem, description.
- `mux_paths` — named mux branches with backend-only `command` strings (executed over SSH on the BMC, never from the browser).

### `i2c_expected_devices.json`

- `buses` — per-bus list of `{ "address", "name", "description" }` for validation highlighting.

Add mux entries only when you have a board-approved BMC selection command.

## Fleet profiles (`config/mgx_arc/`)

### `systems.yaml`

Defines fleet targets: BMC/OS hosts, credential refs, capability profile, and expected profile. Replace `TODO_MGX_ARC_*` placeholders with your lab values on the Spark server (via env vars for secrets).

### `profiles.yaml`

Defines capability and expected profiles:

- **`capability_profiles`** — feature flags (`redfish_power`, `i2c`, `mctp`, `mcu_recovery`, etc.).
- **`profiles`** — per-SKU expectations: USB VID/PID lists, I2C bus scans, MCTP bridges, inventory commands, firmware components, recovery modules.

Commands under `profiles.*.commands` are trusted configuration (scalar strings only):

- `commands.usb.bmc` / `commands.usb.host`
- `commands.mctp`
- `commands.i2c_health`
- `commands.recovery`

Recovery requires both `mcu_recovery: true` in the capability profile **and** `recovery.enabled: true` in the expected profile, with all placeholders removed.

See [config/mgx_arc/README.md](config/mgx_arc/README.md) and [config/mgx_arc/schema.yaml](config/mgx_arc/schema.yaml) for the full schema.

## Lab default credentials (UI prefills)

Some tabs prefill common lab defaults for convenience (not secrets for production):

| Context | Default | Location |
| ------- | ------- | -------- |
| OpenBMC BMC | `root` / `0penBmc` | `app.py`, `app.js` (`BMC_KVM_LOGIN`) |
| OS SSH | `aerial` / `nvidia` | `app.js` (`state.osCreds`) |
| Scope VNC | `Tek_Local_Admin` / `labuser` | `app.js` (`state.scope`) |

Change these for your environment. Users always enter credentials at login for BMC access; OS and scope credentials are entered per-tab.

## Firmware mapping

- `firmware_parts.csv` — CSV mapping of part names to Redfish IDs.
- `MGX_ARC_FIRMWARE_PARTS` in `app.py` — flash metadata and expected image filenames.
- `fw_commands.example.json` — example per-part discovery commands.

Run `python discover_firmware.py <BMC_IP>` on Spark (or a maintainer machine with BMC access) to probe a live system before updating mappings.

## Assets

- `static/img/nvidia-eye.svg` — logo and loading animation.
- Replace or remove if publishing outside NVIDIA branding guidelines you control.
