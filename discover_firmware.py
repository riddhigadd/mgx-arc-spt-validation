#!/usr/bin/env python3
"""
MGX ARC firmware version discovery.

SSH to a BMC (root / 0penBmc), run read-only commands, and print versions for
every MGX ARC firmware part. Use this to figure out which commands work on your
board before filling fw_commands.json.

Usage:
    python discover_firmware.py <BMC_IP>
    python discover_firmware.py 10.137.203.62
    python discover_firmware.py 10.137.203.62 --json > discovered.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys

try:
    import paramiko
except ImportError:
    print("paramiko is required: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

BMC_USER = "root"
BMC_PASS = "0penBmc"
SSH_PORT = 22
SSH_TIMEOUT = 10
CMD_TIMEOUT = 20

# Same checklist as the GUI (app.py).
FIRMWARE_PARTS = [
    {"name": "BMC", "aliases": ["fw_bmc_0", "fw_bmc", "bmc"]},
    {"name": "Host boot firmware (SBIOS/UEFI)", "aliases": ["fw_cpu_0", "sbios", "uefi", "bios"]},
    {"name": "ERoT", "aliases": ["fw_erot_cpu_0", "erot"]},
    {"name": "FPGA/SMR", "aliases": ["fw_fpga_0", "fpga", "smr"]},
    {"name": "CX8 0", "aliases": ["fw_cx8_0", "cx8_0"]},
    {"name": "CX8 1", "aliases": ["fw_cx8_1", "cx8_1"]},
    {"name": "CX8 2", "aliases": ["fw_cx8_2", "cx8_2"]},
    {"name": "GPU VBIOS / GPU Firmware", "aliases": ["fw_gpu_0", "vbios", "gpu"]},
    {"name": "GPU MCU", "aliases": ["fw_gpu_sma_0", "gpu sma", "gpu mcu"]},
    {"name": "HPM MCU 0", "aliases": ["fw_hpm_sma_0", "hpm_sma_0", "hpm mcu 0"]},
    {"name": "HPM MCU 1", "aliases": ["fw_hpm_sma_1", "hpm_sma_1", "hpm mcu 1"]},
    {"name": "MCU FW", "aliases": ["mcu", "fw_mcu"]},
    {"name": "MAC Provisioning FW", "aliases": ["mac", "fw_mac", "provisioning"]},
    {"name": "SMA MCU 0", "aliases": ["sma_mcu_0", "fw_sma_mcu_0"]},
    {"name": "SMA MCU 1", "aliases": ["sma_mcu_1", "fw_sma_mcu_1"]},
    {"name": "HPM MCU", "aliases": ["hpm mcu", "fw_hpm_mcu"]},
    {"name": "GPU MCU", "aliases": ["gpu mcu", "fw_gpu_mcu"]},
]

# Broad discovery — run once, then match aliases to parts.
DISCOVERY_BUNDLE = r"""
set +e
echo "=== HOST ==="
hostname
echo "=== OS RELEASE ==="
. /etc/os-release 2>/dev/null; echo "VERSION_ID=${VERSION_ID:-}"; echo "VERSION=${VERSION:-}"; echo "OPENBMC_TARGET_MACHINE=${OPENBMC_TARGET_MACHINE:-}"
echo "=== KERNEL ==="
uname -a
echo "=== TOOLS ON PATH ==="
for t in nvfwupd fw-util fw_printenv pldmtool busctl curl redfish-tool obmcutil; do
  command -v "$t" 2>/dev/null && echo "FOUND $t"
done
echo "=== BUSCTL SOFTWARE TREE ==="
for svc in xyz.openbmc_project.Software.BMC.Updater xyz.openbmc_project.Software.Host.Updater; do
  echo "-- service: $svc"
  busctl tree "$svc" 2>/dev/null
done
echo "=== BUSCTL SOFTWARE VERSIONS ==="
for svc in xyz.openbmc_project.Software.BMC.Updater xyz.openbmc_project.Software.Host.Updater; do
  busctl tree "$svc" 2>/dev/null | grep -oE '/xyz/openbmc_project/software/[^ ]+' | sort -u | while read -r p; do
    id="${p##*/}"
    ver=$(busctl get-property "$svc" "$p" xyz.openbmc_project.Software.Version Version 2>/dev/null | cut -d '"' -f2)
    pur=$(busctl get-property "$svc" "$p" xyz.openbmc_project.Software.Version Purpose 2>/dev/null | cut -d '"' -f2)
    echo "SOFTWARE|$id|${pur##*.}|$ver"
  done
done
echo "=== REDFISH FIRMWARE INVENTORY (localhost) ==="
curl -sk -u root:0penBmc https://localhost/redfish/v1/UpdateService/FirmwareInventory 2>/dev/null | head -c 8000
echo
echo "=== REDFISH SOFTWARE INVENTORY MEMBERS ==="
curl -sk -u root:0penBmc https://localhost/redfish/v1/UpdateService/FirmwareInventory 2>/dev/null | \
  grep -oE '"/redfish/v1/UpdateService/FirmwareInventory/[^"]+"' | tr -d '"' | while read -r m; do
  curl -sk -u root:0penBmc "https://localhost$m" 2>/dev/null | \
    grep -E '"Name"|"Version"|"Id"' | tr -d ' ' | paste - - - 2>/dev/null
done
echo "=== NVFWUPD (if present) ==="
nvfwupd version 2>/dev/null
nvfwupd list 2>/dev/null
nvfwupd show_version 2>/dev/null
echo "=== FW-UTIL (if present) ==="
fw-util version 2>/dev/null
fw-util versions 2>/dev/null
fw-util list 2>/dev/null
echo "=== PLDM FW QUERY (if present) ==="
pldmtool fw_update query 2>/dev/null | head -n 80
echo "=== D-BUS SOFTWARE INTROSPECT SAMPLE ==="
busctl introspect xyz.openbmc_project.Software.BMC.Updater /xyz/openbmc_project/software 2>/dev/null | head -n 40
echo "=== END DISCOVERY ==="
"""

# Per-part candidate commands (tried in order; first non-empty stdout wins).
PART_CANDIDATE_COMMANDS: dict[str, list[str]] = {
    "BMC": [
        '. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-$VERSION}"',
        "cat /etc/version 2>/dev/null | head -n1",
    ],
    "Host boot firmware (SBIOS/UEFI)": [
        "busctl get-property xyz.openbmc_project.Software.Host.Updater "
        "/xyz/openbmc_project/software/UEFI xyz.openbmc_project.Software.Version Version 2>/dev/null | cut -d '\"' -f2",
        "busctl get-property xyz.openbmc_project.Software.Host.Updater "
        "/xyz/openbmc_project/software/BIOS xyz.openbmc_project.Software.Version Version 2>/dev/null | cut -d '\"' -f2",
        "curl -sk -u root:0penBmc https://localhost/redfish/v1/Systems/system 2>/dev/null | grep -i BiosVersion",
    ],
    "ERoT": [
        "busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i erot",
        "curl -sk -u root:0penBmc https://localhost/redfish/v1/UpdateService/FirmwareInventory 2>/dev/null | grep -i erot",
    ],
    "FPGA/SMR": [
        "busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -iE 'fpga|smr'",
    ],
    "CX8 0": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i cx8"],
    "CX8 1": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i cx8"],
    "CX8 2": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i cx8"],
    "GPU VBIOS / GPU Firmware": [
        "busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -iE 'gpu|vbios'",
        "curl -sk -u root:0penBmc https://localhost/redfish/v1/UpdateService/FirmwareInventory 2>/dev/null | grep -iE 'gpu|vbios'",
    ],
    "GPU MCU": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i gpu_sma"],
    "HPM MCU 0": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i hpm_sma"],
    "HPM MCU 1": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i hpm_sma"],
    "MCU FW": [
        "busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i mcu",
        "nvfwupd list 2>/dev/null | grep -i mcu",
    ],
    "MAC Provisioning FW": [
        "busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -iE 'mac|provision'",
    ],
    "SMA MCU 0": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i sma_mcu"],
    "SMA MCU 1": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i sma_mcu"],
    "HPM MCU": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i hpm_mcu"],
    "GPU MCU": ["busctl tree xyz.openbmc_project.Software.Host.Updater 2>/dev/null | grep -i gpu_mcu"],
}


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def ssh_connect(bmc_ip: str):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        bmc_ip,
        port=SSH_PORT,
        username=BMC_USER,
        password=BMC_PASS,
        timeout=SSH_TIMEOUT,
        banner_timeout=SSH_TIMEOUT,
        auth_timeout=SSH_TIMEOUT,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def ssh_run(client, command: str) -> tuple[int, str, str]:
    _stdin, stdout, stderr = client.exec_command(command, timeout=CMD_TIMEOUT)
    out = stdout.read().decode("utf-8", "replace").strip()
    err = stderr.read().decode("utf-8", "replace").strip()
    code = stdout.channel.recv_exit_status()
    return code, out, err


def parse_software_lines(bundle_out: str) -> list[dict]:
    items = []
    for line in bundle_out.splitlines():
        if not line.startswith("SOFTWARE|"):
            continue
        _, obj_id, purpose, version = line.split("|", 3)
        items.append({"id": obj_id, "purpose": purpose, "version": version})
    return items


def match_part(part: dict, software: list[dict]) -> str | None:
    keys = [norm(part["name"]), *[norm(a) for a in part.get("aliases", [])]]
    for sw in software:
        blob = norm(f"{sw['id']} {sw['purpose']}")
        if any(k and k in blob for k in keys):
            return sw["version"]
    return None


def discover(bmc_ip: str) -> dict:
    client = ssh_connect(bmc_ip)
    try:
        _code, bundle_out, _err = ssh_run(client, DISCOVERY_BUNDLE)
        software = parse_software_lines(bundle_out)

        parts = []
        for part in FIRMWARE_PARTS:
            version = match_part(part, software) or ""
            winning_cmd = None

            if not version:
                for cmd in PART_CANDIDATE_COMMANDS.get(part["name"], []):
                    _c, out, _e = ssh_run(client, cmd)
                    line = out.splitlines()[0].strip() if out else ""
                    if line and "grep" not in cmd:  # skip raw grep path listings
                        version = line
                        winning_cmd = cmd
                        break
                    if line and "/" in line and "software" in line.lower():
                        # grep found a dbus path — try to read Version property
                        path = line.strip().split()[-1]
                        read_cmd = (
                            f"busctl get-property xyz.openbmc_project.Software.Host.Updater "
                            f"\"{path}\" xyz.openbmc_project.Software.Version Version 2>/dev/null "
                            "| cut -d '\"' -f2"
                        )
                        _c2, ver_out, _e2 = ssh_run(client, read_cmd)
                        if ver_out.strip():
                            version = ver_out.strip()
                            winning_cmd = read_cmd
                            break

            if not version:
                version = match_part(part, software) or ""

            parts.append(
                {
                    "name": part["name"],
                    "version": version,
                    "command": winning_cmd,
                    "source": "busctl" if version and not winning_cmd else ("command" if winning_cmd else "not found"),
                }
            )

        return {
            "bmc_ip": bmc_ip,
            "software_inventory": software,
            "parts": parts,
            "raw_discovery": bundle_out,
        }
    finally:
        client.close()


def print_report(result: dict) -> None:
    print(f"\n{'=' * 72}")
    print(f"MGX ARC Firmware Discovery — {result['bmc_ip']}")
    print(f"{'=' * 72}\n")

    print("SOFTWARE INVENTORY (busctl):")
    if result["software_inventory"]:
        for sw in result["software_inventory"]:
            print(f"  {sw['id']:30} {sw['purpose']:12} {sw['version']}")
    else:
        print("  (none found)")

    print(f"\n{'-' * 72}")
    print(f"{'Firmware Part':<35} {'Version':<25} {'Source'}")
    print(f"{'-' * 72}")
    for p in result["parts"]:
        ver = p["version"] or "—"
        print(f"{p['name']:<35} {ver:<25} {p['source']}")
        if p.get("command"):
            print(f"  -> command: {p['command']}")

    print(f"\n{'-' * 72}")
    print("Suggested fw_commands.json entries (parts with a working command):")
    suggested = [
        {"name": p["name"], "command": p["command"]}
        for p in result["parts"]
        if p.get("command")
    ]
    if suggested:
        print(json.dumps(suggested, indent=2))
    else:
        print("  (no per-part commands resolved yet — check raw discovery below)")

    print(f"\n{'=' * 72}")
    print("RAW DISCOVERY OUTPUT")
    print(f"{'=' * 72}")
    print(result["raw_discovery"])


def main():
    parser = argparse.ArgumentParser(description="Discover MGX ARC firmware versions over SSH")
    parser.add_argument("bmc_ip", help="BMC IP address")
    parser.add_argument("--json", action="store_true", help="Print JSON only (no human report)")
    args = parser.parse_args()

    try:
        result = discover(args.bmc_ip)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
