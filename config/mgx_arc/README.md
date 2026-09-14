# MGX ARC configuration

`backend.config.load_config()` loads `systems.yaml` and `profiles.yaml` from
this directory and validates the merged version 1 configuration. `schema.yaml`
documents the complete file shape.

## Credentials

Credentials are read only from environment variables. Set these before
connecting to a target:

- `MGX_ARC_BMC_USERNAME`
- `MGX_ARC_BMC_PASSWORD`
- `MGX_ARC_HOST_USERNAME`
- `MGX_ARC_HOST_PASSWORD`

Never add usernames, passwords, tokens, or other secrets to YAML. To use the
optional jump host, add another credential entry containing only
`username_env` and `password_env`, replace its TODO credential reference and
hostname, then set a target's `jump_host_ref` to `lab-jump`.

To load another directory, set `MGX_ARC_CONFIG_DIR`.

## Placeholders and recovery

Replace every applicable `TODO_MGX_ARC_*` value with a lab-approved hostname,
revision, expectation, or command. Hosts containing a placeholder are reported
as `not_configured`. Placeholder commands are never executed.

Recovery has two independent safety gates: `mcu_recovery` in the capability
profile and `recovery.enabled` in the expected profile. Both remain `false`
while the sample recovery module or command contains placeholders. Enable them
only after the complete sequence has been reviewed and all placeholders have
been removed.

## Command keys

Commands are trusted configuration and must remain scalar strings at these
exact paths:

- `commands.usb.bmc`
- `commands.usb.host`
- `commands.mctp`
- `commands.i2c_health`
- `commands.recovery`

The USB, MCTP, I2C, inventory, firmware, and recovery sections hold expected
topology and comparison metadata; they are not client-supplied shell commands.

## Lab-grounded expectations (EVT 2026-09-03)

USB, I2C, and OS PCIe/NVMe maps are filled from PowerCycling inventory on
`aerial-mgx-evt-01` / BMC `mgx-arc`:

| Flow | Commands | Config |
| ---- | -------- | ------ |
| OS_INV | `lspci`, `lsusb`, `nvme --list` | `usb_golden_map.json` view `os`, `os_inventory_map.json` |
| BMC_INV | `lsusb`, `lsusb -tv`, `i2cdetect -y <bus>` | `usb_golden_map.json` view `bmc`, `i2c_bus_map.json`, `i2c_expected_devices.json` |

BMC USB and host USB are separate views. Do not expect FT4232H on the BMC or
NVIDIA `0955:cf11` MCTP devices on the host.

I2C role names remain descriptive until a schematic I2C map is locked. Mux
selection commands are still omitted. Recovery, MCTP EIDs, FRU commands, AC
cycle, and node BMC/OS IPs stay `TODO_MGX_ARC_*`.

## Existing JSON compatibility

If a profile does not populate `usb.golden_map`,
`i2c.bus_map`, or `i2c.expected_devices`, the loader reads the repository's
existing `usb_golden_map.json`, `i2c_bus_map.json`, and
`i2c_expected_devices.json` into those keys. Explicit non-empty YAML values
take precedence. The JSON files are not modified.
