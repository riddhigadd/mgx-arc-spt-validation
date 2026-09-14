"""
MGX ARC GUI
===========
A web dashboard for connecting to and managing multiple MGX ARC systems.

Each MGX ARC system exposes an OpenBMC baseboard management controller (BMC).
This backend acts as a thin, safe proxy between the browser and each BMC:
  - Power control and sensors use the Redfish REST API.
  - The overview summary is gathered by running read-only commands on the BMC over SSH.
  - Firmware versions are read from BMC Redfish FirmwareInventory (FW_BMC_0, etc.).

BMC access is restricted to authorized credentials. Invalid logins are rejected
before we ever hit the network.
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import struct
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from base64 import b64encode

import requests
import urllib3
from flask import Flask, jsonify, request, send_from_directory
from flask_sock import Sock
from requests.auth import HTTPBasicAuth

from backend.api import initialize_arc_api
from backend.sso import init_sso

# BMCs use self-signed certificates on the management network.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Authorized BMC credentials (enforced server-side; not exposed in the UI).
REQUIRED_BMC_USER = "root"
REQUIRED_BMC_PASSWORD = "0penBmc"

# Network timeouts (seconds) for talking to a BMC.
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15

# Firmware uploads (multipart) can stall the socket for a while as the BMC
# receives/erases flash. This is the socket-inactivity timeout for the upload
# POST. NVIDIA BMCs return a 202 + Task quickly, so if we get no response in
# this window we assume the response was lost and fall back to following the
# task the BMC started (see _recover_via_task). Override with FLASH_UPLOAD_TIMEOUT.
UPLOAD_CONNECT_TIMEOUT = 15
UPLOAD_TIMEOUT = int(os.environ.get("FLASH_UPLOAD_TIMEOUT", "900"))

# A failed client-side TLS write usually still means the BMC received the image
# and started a task, so these are treated as "check the task" rather than as a
# hard failure. SSLWantWriteError shows up on slow/congested BMC links.
UPLOAD_INTERRUPTED_MARKERS = (
    "write operation timed out",
    "did not complete (write)",
    "sslwantwriteerror",
    "connection aborted",
    "connection reset",
    "broken pipe",
    "aborted",
)

# Redfish inventory is many small GETs; read them in parallel over pooled
# keep-alive connections instead of one TLS handshake per component.
REDFISH_PARALLEL_READS = 8

# SSH is used to gather firmware versions and the overview summary by running
# read-only commands on the BMC. The raw commands/output are never shown in the
# UI; only the parsed results are returned.
SSH_PORT = 22
SSH_TIMEOUT = 8
SSH_CMD_TIMEOUT = 12
# All overview fields run in one exec, so allow more than a single command.
OVERVIEW_BATCH_TIMEOUT = 30

# How long a Redfish task may sit at PercentComplete=100 while still reporting
# TaskState="Running" before we treat the image as written and move on to
# activation. These BMCs reboot to activate and never post a terminal state.
TASK_ACTIVATION_GRACE = int(os.environ.get("TASK_ACTIVATION_GRACE", "45"))

# Redfish TaskStates the BMC still counts as an update in flight.
ACTIVE_TASK_STATES = ("new", "starting", "running", "pending", "service", "suspended")

# UpdateService rejects a new image while it thinks one is still being applied.
UPDATE_IN_PROGRESS_MARKERS = (
    "update is in progress",
    "update in progress",
    "updateinprogress",
    "another update",
    "already in progress",
)
# /run/initramfs/update writes the whole BMC flash and can take several minutes.
BMC_UPDATE_TIMEOUT = int(os.environ.get("BMC_UPDATE_TIMEOUT", "600"))
# MGX ARC exposes the SMA/FPGA SPI NOR as the stable fpga1 MTD device.
SPI_READ_DEVICE = "/dev/mtd/by-name/fpga1"
SPI_READ_IMAGE = "/tmp/u1958_read.bin"
SPI_READ_TIMEOUT = int(os.environ.get("SPI_READ_TIMEOUT", "900"))
USB_ENUM_TIMEOUT = int(os.environ.get("USB_ENUM_TIMEOUT", "30"))
PCIE_LSPCI_TIMEOUT = int(os.environ.get("PCIE_LSPCI_TIMEOUT", "30"))
USB_GOLDEN_MAP_FILE = os.path.join(BASE_DIR, "usb_golden_map.json")
OS_INVENTORY_MAP_FILE = os.path.join(BASE_DIR, "os_inventory_map.json")
# Network oscilloscopes speak SCPI over TCP (Keysight 5025, Tek 4000, etc.).
SCOPE_SCPI_PORTS = tuple(
    int(p) for p in os.environ.get("SCOPE_SCPI_PORTS", "5025,4000,5555").split(",")
    if str(p).strip().isdigit()
) or (5025, 4000, 5555)
SCOPE_CONNECT_TIMEOUT = float(os.environ.get("SCOPE_CONNECT_TIMEOUT", "4"))
SCOPE_IO_TIMEOUT = float(os.environ.get("SCOPE_IO_TIMEOUT", "12"))
SCOPE_VNC_PORT = int(os.environ.get("SCOPE_VNC_PORT", "5900"))
SCOPE_VNC_TOKEN_TTL = 60
SCOPE_WAVE_POINTS = int(os.environ.get("SCOPE_WAVE_POINTS", "1000"))
# I2C tools are run over SSH. Scans and dumps can be slower on muxed buses.
I2C_CMD_TIMEOUT = int(os.environ.get("I2C_CMD_TIMEOUT", "60"))
I2C_BUS_MAP_FILE = os.path.join(BASE_DIR, "i2c_bus_map.json")
I2C_EXPECTED_DEVICES_FILE = os.path.join(BASE_DIR, "i2c_expected_devices.json")

# Overview summary: (label, shell command). Each command should print a single
# clean value. Failures are tolerated and shown as "—".
OVERVIEW_COMMANDS = [
    ("Host Name", "hostname 2>/dev/null"),
    ("Model", "cat /sys/firmware/devicetree/base/model 2>/dev/null | tr -d '\\000' "
              "|| { . /etc/os-release 2>/dev/null; echo \"$OPENBMC_TARGET_MACHINE\"; }"),
    ("BMC Version", ". /etc/os-release 2>/dev/null; echo \"${VERSION_ID:-$VERSION}\""),
    ("Power State", "obmcutil power status 2>/dev/null | awk -F': ' 'NR==1{print $2}' "
                    "|| busctl get-property xyz.openbmc_project.State.Chassis "
                    "/xyz/openbmc_project/state/chassis0 xyz.openbmc_project.State.Chassis "
                    "CurrentPowerState 2>/dev/null | awk -F. '{print $NF}' | tr -d '\"'"),
    ("Kernel", "uname -r 2>/dev/null"),
    ("BMC Uptime", "uptime -p 2>/dev/null || uptime 2>/dev/null"),
    ("BMC Time", "date 2>/dev/null"),
]

# MGX ARC firmware parts — versions read via Redfish FirmwareInventory (same
# approach as BMC). See generated-table CSV for command reference.
MGX_ARC_FIRMWARE_PARTS = [
    {
        "name": "BMC",
        "redfish_id": "FW_BMC_0",
        "match_keywords": ["BMC"],
        "notes": "Full mgxa version string from Redfish inventory.",
    },
    {
        "name": "Host boot firmware (SBIOS/UEFI)",
        "redfish_id": "FW_CPU_0",
        "match_keywords": ["CPU"],
        "host_cmd": "sudo dmidecode -t 45 2>/dev/null | awk -F': ' '/Version:/{print $2; exit}'",
        "notes": "Redfish FW_CPU_0; falls back to host dmidecode if host IP is set.",
    },
    {"name": "ERoT", "redfish_id": "FW_ERoT_CPU_0", "match_keywords": ["EROT"]},
    {"name": "FPGA / SMR", "redfish_id": "FW_FPGA_0", "match_keywords": ["FPGA"]},
    {"name": "CX8 0", "redfish_id": "FW_CX8_0", "match_keywords": ["CX8", "0"]},
    {"name": "CX8 1", "redfish_id": "FW_CX8_1", "match_keywords": ["CX8", "1"]},
    {"name": "CX8 2", "redfish_id": "FW_CX8_2", "match_keywords": ["CX8", "2"]},
    {
        "name": "GPU VBIOS / GPU Firmware",
        "redfish_id": "FW_GPU_0",
        "match_keywords": ["GPU"],
        "exclude_keywords": ["SMA"],
    },
    {
        "name": "GPU MCU",
        "redfish_id": "FW_GPU_SMA_0",
        "match_keywords": ["GPU", "SMA"],
        "notes": "GPU-side MCU firmware.",
    },
    {"name": "HPM MCU 0", "redfish_id": "FW_HPM_SMA_0", "match_keywords": ["HPM", "SMA", "0"]},
    {"name": "HPM MCU 1", "redfish_id": "FW_HPM_SMA_1", "match_keywords": ["HPM", "SMA", "1"]},
]
FIRMWARE_IMAGES_DIR = os.path.join(BASE_DIR, "firmware_images")

# BMC flash targets for MGX ARC (place image files in firmware_images/).
BMC_FLASH_TARGETS = [
    {
        "id": "erot",
        "label": "BMC w/ ERoT",
        "version": "mgxa-2606-24.00",
        "method": "redfish_multipart",
        "image_file": "mgxa-2606-24.00.image",
        "description": "Redfish multipart update (ForceUpdate). BMC stays reachable during upload.",
    },
    {
        "id": "no_erot_bin",
        "label": "BMC w/o ERoT (.bin) — first-time / manual",
        "version": "mgxa-2606-24.01",
        "method": "initramfs_scp",
        "image_file": "MGX_ARC_26062401_dev_debug.signed-apimage.bin",
        "description": (
            "Manual bring-up procedure: SCP .bin to /run/initramfs/image-bmc, verify "
            "md5sum, run /run/initramfs/update (or reboot-to-flash fallback), then cold "
            "power cycle. Requires the raw SCP flash image (e.g. *_scp_image.bin) — a "
            "signed-apimage/Redfish .bin will fail with 'Unable to find mtd partition'."
        ),
    },
    {
        "id": "no_erot_fwpkg",
        "label": "BMC w/o ERoT (.fwpkg) — GUI / Redfish",
        "version": "mgxa-2606-24.01",
        "method": "redfish_multipart",
        "image_file": "MGX_ARC_26062401_dev_debug.fwpkg",
        "description": (
            "Recommended for GUI flashing: Redfish update-multipart (same as curl -F "
            "UpdateParameters / UpdateFile). Progress is shown in the side panel."
        ),
        "recommended": True,
    },
]

# FPGA / SMR flash target (Redfish multipart → FW_FPGA_0).
SMR_FLASH_TARGET = {
    "id": "smr",
    "label": "FPGA / SMR",
    "version": "0V0C",
    "redfish_id": "FW_FPGA_0",
    "redfish_target": "/redfish/v1/UpdateService/FirmwareInventory/FW_FPGA_0",
    "image_file": "FPGA_arc_0v0C_signed-apimage.bin",
    "description": "Flash signed SMR/FPGA image via Redfish update-multipart (ForceUpdate, target FW_FPGA_0).",
    "flash_label": "SMR",
}

# ERoT flash target (Redfish multipart → FW_ERoT_CPU_0).
EROT_FLASH_TARGET = {
    "id": "erot",
    "label": "ERoT",
    "version": "01.04.0037.0000_nc00",
    "redfish_id": "FW_ERoT_CPU_0",
    "redfish_target": "/redfish/v1/UpdateService/FirmwareInventory/FW_ERoT_CPU_0",
    "image_file": "cec1736-ecfw-01.04.0037.0000-nc00-rel-prod.bin",
    "description": (
        "Flash ERoT ecfw image (NVIDIA RevB) via Redfish update-multipart "
        "(ForceUpdate, target FW_ERoT_CPU_0)."
    ),
    "flash_label": "ERoT",
}

# SBIOS / UEFI flash target (Redfish multipart → FW_CPU_0).
SBIOS_FLASH_TARGET = {
    "id": "sbios",
    "label": "SBIOS / UEFI",
    "version": "02.06.06",
    "redfish_id": "FW_CPU_0",
    "redfish_target": "/redfish/v1/UpdateService/FirmwareInventory/FW_CPU_0",
    "image_file": "SBIOS_P4180_PG535_P4180_SKU_893_NC00_02.06.06_rel_prod.fwpkg",
    "description": (
        "Flash host boot firmware (SBIOS/UEFI) via Redfish update-multipart "
        "(ForceUpdate, target FW_CPU_0)."
    ),
    "flash_label": "SBIOS",
}

# FML bundle (.fwpkg) — multi-component nvfw update via Redfish multipart.
FML_BUNDLE_FLASH = {
    "id": "fml_bundle",
    "label": "FML Bundle",
    "bundle_id": "Grace-CPU-P4180_0017_260603.1.1",
    "image_file": "nvfw_Grace-CPU-P4180_0017_260603.1.1_custom_prod-signed.fwpkg",
    "description": (
        "Prod-signed nvfw FML bundle. Updates ERoT, BMC, GPU, SBIOS, MCU, and SMR "
        "in one flash via Redfish update-multipart (ForceUpdate, all targets)."
    ),
    "components": [
        {
            "part": "ERoT", "vendor": "NVIDIA", "model": "NC00",
            "version": "01.04.0037.0000_nc00", "signature": "Prod-Signed",
        },
        {
            "part": "BMC", "vendor": "NVIDIA", "model": "P3809",
            "version": "mgxa-2606-03.01", "signature": "Debug-Signed",
        },
        {
            "part": "GPU", "vendor": "NVIDIA", "model": "PG147-SKU210",
            "version": "98.03.BB.00.01", "signature": "Prod-Signed",
        },
        {
            "part": "SBIOS", "vendor": "NVIDIA", "model": "P4180_SKU_893_NC00",
            "version": "02.06.05", "signature": "Prod-Signed",
        },
        {
            "part": "MCU", "vendor": "NVIDIA", "model": "P4180-MCXN236",
            "version": "0036.00.0285.0000", "signature": "Prod-Signed",
        },
        {
            "part": "MCU", "vendor": "NVIDIA", "model": "PG147",
            "version": "0008.00.0272.0000", "signature": "Prod-Signed",
        },
        {
            "part": "SMR", "vendor": "NVIDIA", "model": "P4180",
            "version": "0.0C", "signature": "Debug-Signed",
        },
    ],
}

# ConnectX8 flash — PK vs QP variants, latest 40.97.5444 (.fwpkg).
CX8_FLASH_TARGETS = [
    {
        "id": "pk",
        "label": "CX8 PK",
        "variant": "PK",
        "platform": "P4180 MRG ARC PK Ax",
        "version": "40.97.5444",
        "uefi": "14.41.14",
        "flexboot": "3.9.101",
        "nvid": "NVD0000000141",
        "image_file": "fw-ConnectX8-PK-40.97.5444.fwpkg",
        "source_file": (
            "fw-ConnectX8-rel-40_97_5444-cx8_P4180_MRG_ARC_PK_Ax-UEFI-14.41.14-"
            "FlexBoot-3.9.101.signed-NVD0000000141.fwpkg"
        ),
        "description": (
            "ConnectX8 PK package for MGX ARC PK boards. "
            "Flashes FW_CX8_0/1/2 to 40.97.5444 (UEFI 14.41.14, FlexBoot 3.9.101)."
        ),
    },
    {
        "id": "qp",
        "label": "CX8 QP",
        "variant": "QP",
        "platform": "P4180 MGX ARC QP Ax",
        "version": "40.97.5444",
        "uefi": "14.41.14",
        "flexboot": "3.9.101",
        "nvid": "NVD0000000138",
        "image_file": "fw-ConnectX8-QP-40.97.5444.fwpkg",
        "source_file": (
            "fw-ConnectX8-rel-40_97_5444-cx8_P4180_MGX_ARC_QP_Ax-UEFI-14.41.14-"
            "FlexBoot-3.9.101-NVD0000000138.fwpkg"
        ),
        "description": (
            "ConnectX8 QP package for MGX ARC QP boards. "
            "Flashes FW_CX8_0/1/2 to 40.97.5444 (UEFI 14.41.14, FlexBoot 3.9.101)."
        ),
    },
]
CX8_REDFISH_IDS = ["FW_CX8_0", "FW_CX8_1", "FW_CX8_2"]

app = Flask(__name__, static_folder="static", static_url_path="/static")
sock = Sock(app)

# The GUI is updated in place on the test bench, so a browser holding an old
# app.js while the server runs new routes shows confusing 404s. Always revalidate.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
arc_api = initialize_arc_api(app)
init_sso(app)

_scope_vnc_tokens = {}
_scope_vnc_tokens_lock = threading.Lock()


@app.after_request
def _no_cache_static(response):
    if request.path.startswith("/static") or request.path == "/":
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response

# ---------------------------------------------------------------------------
# Redfish helpers
# ---------------------------------------------------------------------------
def _bmc_base(bmc_ip):
    return f"https://{bmc_ip}/redfish/v1"


def _bmc_auth():
    return HTTPBasicAuth(REQUIRED_BMC_USER, REQUIRED_BMC_PASSWORD)


_redfish_local = threading.local()


def _redfish_session():
    """Per-thread pooled session so repeated Redfish reads reuse the TLS
    handshake instead of reconnecting for every request."""
    session = getattr(_redfish_local, "session", None)
    if session is None:
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4,
            pool_maxsize=REDFISH_PARALLEL_READS,
            max_retries=0,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _redfish_local.session = session
    return session


def _redfish_get(bmc_ip, path):
    """GET a Redfish resource. Returns (ok, data_or_error, status_code)."""
    url = f"{_bmc_base(bmc_ip)}{path}"
    try:
        resp = _redfish_session().get(
            url,
            auth=_bmc_auth(),
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.exceptions.ConnectTimeout:
        return False, "Connection timed out reaching the BMC.", 504
    except requests.exceptions.ConnectionError:
        return False, "Could not reach the BMC on the network.", 502
    except requests.exceptions.RequestException as exc:
        return False, f"Request failed: {exc}", 502

    if resp.status_code == 401:
        return False, "BMC rejected the credentials.", 401
    if resp.status_code >= 400:
        return False, f"BMC returned HTTP {resp.status_code}.", resp.status_code
    try:
        return True, resp.json(), resp.status_code
    except ValueError:
        return False, "BMC returned a non-JSON response.", 502


def _redfish_post(bmc_ip, path, payload):
    url = f"{_bmc_base(bmc_ip)}{path}"
    try:
        resp = _redfish_session().post(
            url,
            auth=_bmc_auth(),
            json=payload,
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.exceptions.RequestException as exc:
        return False, f"Request failed: {exc}", 502

    if resp.status_code == 401:
        return False, "BMC rejected the credentials.", 401
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = body.get("error", {}).get("message", "")
        except ValueError:
            detail = resp.text[:200]
        return False, f"BMC returned HTTP {resp.status_code}. {detail}".strip(), resp.status_code
    return True, {"status": resp.status_code}, resp.status_code


def _redfish_delete(bmc_ip, path):
    url = f"{_bmc_base(bmc_ip)}{path}"
    try:
        resp = _redfish_session().delete(
            url,
            auth=_bmc_auth(),
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except requests.exceptions.RequestException as exc:
        return False, f"Request failed: {exc}", 502
    return resp.status_code < 400, resp.text[:200], resp.status_code


def _validate_bmc_credentials(username, password):
    """Return an error string if the credentials are not authorized."""
    if username != REQUIRED_BMC_USER or password != REQUIRED_BMC_PASSWORD:
        return "Invalid BMC credentials."
    return None


def _require_bmc_session():
    """Shared guard: BMC address + authorized credentials in request headers."""
    bmc_ip = request.headers.get("X-BMC-Host", "").strip()
    host_ip = request.headers.get("X-Host-IP", "").strip()
    username = request.headers.get("X-BMC-User", "")
    password = request.headers.get("X-BMC-Pass", "")
    if not bmc_ip:
        return None, (jsonify({"error": "BMC address is required."}), 400)
    cred_error = _validate_bmc_credentials(username, password)
    if cred_error:
        return None, (jsonify({"error": cred_error}), 403)
    return {"bmc_ip": bmc_ip, "host_ip": host_ip}, None


# ---------------------------------------------------------------------------
# SSH helpers (used to run read-only commands on the BMC behind the scenes)
# ---------------------------------------------------------------------------
def _ssh_connect(host_ip, username=None, password=None, label="BMC"):
    """Open an SSH connection. Returns (client, error_string).

    Defaults to BMC root credentials when username/password are omitted.
    """
    try:
        import paramiko
    except ImportError:
        return None, ("paramiko is not installed. Run "
                      "'pip install -r requirements.txt' and restart the app.")

    user = (username or REQUIRED_BMC_USER).strip() or REQUIRED_BMC_USER
    passwd = password if password is not None else REQUIRED_BMC_PASSWORD
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host_ip,
            port=SSH_PORT,
            username=user,
            password=passwd,
            timeout=SSH_TIMEOUT,
            banner_timeout=SSH_TIMEOUT,
            auth_timeout=SSH_TIMEOUT,
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.AuthenticationException:
        return None, f"{label} rejected the SSH credentials."
    except paramiko.SSHException as exc:
        return None, f"SSH error reaching the {label}: {exc}"
    except OSError as exc:
        return None, f"Could not reach the {label} over SSH: {exc}"
    return client, None


def _ssh_run(client, command, timeout=None):
    """Run one command. Returns (exit_code, stdout, stderr)."""
    try:
        _stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout or SSH_CMD_TIMEOUT
        )
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except Exception as exc:  # noqa: BLE001 - surface any exec failure cleanly
        return 1, "", str(exc)


def _ssh_run_logged(client, command, job_id=None, timeout=None, log_key="terminal_log"):
    """Run SSH command and mirror it to the job terminal log."""
    if job_id:
        _job_log(job_id, f"$ {command}", "cmd", log_key=log_key)
    code, out, err = _ssh_run(client, command, timeout=timeout)
    if job_id:
        for line in (out or "").splitlines():
            _job_log(job_id, line, "out", log_key=log_key)
        for line in (err or "").splitlines():
            _job_log(job_id, line, "err", log_key=log_key)
    return code, out, err


def _ssh_run_streaming(client, command, job_id=None, timeout=None):
    """Run a long SSH command and stream stdout/stderr into the terminal log."""
    if job_id:
        _job_log(job_id, f"$ {command}", "cmd")
    out_parts = []
    err_parts = []
    try:
        _stdin, stdout, stderr = client.exec_command(
            command, timeout=timeout or SSH_CMD_TIMEOUT
        )
        channel = stdout.channel
        while True:
            if channel.recv_ready():
                chunk = stdout.read(4096).decode("utf-8", "replace")
                if chunk:
                    for line in chunk.splitlines():
                        line = line.rstrip("\r")
                        if line:
                            out_parts.append(line)
                            if job_id:
                                _job_log(job_id, line, "out")
            if channel.recv_stderr_ready():
                chunk = stderr.read(4096).decode("utf-8", "replace")
                if chunk:
                    for line in chunk.splitlines():
                        line = line.rstrip("\r")
                        if line:
                            err_parts.append(line)
                            if job_id:
                                _job_log(job_id, line, "err")
            if channel.exit_status_ready():
                break
            time.sleep(0.12)
        # Drain any remaining output after exit.
        for stream, parts, style in (
            (stdout, out_parts, "out"),
            (stderr, err_parts, "err"),
        ):
            rest = stream.read().decode("utf-8", "replace")
            if rest:
                for line in rest.splitlines():
                    line = line.rstrip("\r")
                    if line:
                        parts.append(line)
                        if job_id:
                            _job_log(job_id, line, style)
        code = channel.recv_exit_status()
        return code, "\n".join(out_parts), "\n".join(err_parts)
    except Exception as exc:  # noqa: BLE001
        if job_id:
            _job_log(job_id, str(exc), "err")
        return 1, "\n".join(out_parts), str(exc)


def _fetch_firmware_inventory(bmc_ip):
    """Return {FW_BMC_0: {version, name, health}, ...} from Redfish inventory."""
    ok, inv, _status = _redfish_get(bmc_ip, "/UpdateService/FirmwareInventory")
    if not ok:
        return {}, inv

    member_ids = []
    for member in inv.get("Members", []):
        member_id = member.get("@odata.id", "").rsplit("/", 1)[-1]
        if member_id:
            member_ids.append(member_id)

    def _read_member(member_id):
        ok_c, comp, _ = _redfish_get(
            bmc_ip, f"/UpdateService/FirmwareInventory/{member_id}"
        )
        return member_id, (comp if ok_c else None)

    inventory = {}
    if member_ids:
        with ThreadPoolExecutor(
            max_workers=min(REDFISH_PARALLEL_READS, len(member_ids))
        ) as pool:
            for member_id, comp in pool.map(_read_member, member_ids):
                if comp is None:
                    continue
                inventory[member_id] = {
                    "version": comp.get("Version") or "",
                    "name": comp.get("Name") or member_id,
                    "health": (comp.get("Status") or {}).get("Health"),
                    "description": comp.get("Description") or "",
                }
    return inventory, None


def _match_inventory_member(part, inventory):
    """Find the Redfish inventory member for a configured part.

    Tries an exact redfish_id match first, then falls back to a fuzzy match
    on member id / name using keyword sets (BMCs vary in how they name CX8,
    GPU, SMA members). Returns (member_id, entry) or (None, None).
    """
    exact = part.get("redfish_id", "")
    if exact and exact in inventory:
        return exact, inventory[exact]

    keywords = [k.upper() for k in part.get("match_keywords", [])]
    excludes = [k.upper() for k in part.get("exclude_keywords", [])]
    if not keywords:
        return None, None

    for member_id, entry in inventory.items():
        haystack = f"{member_id} {entry.get('name', '')}".upper()
        if any(x in haystack for x in excludes):
            continue
        if all(k in haystack for k in keywords):
            return member_id, entry
    return None, None


def _read_host_version(host_ip, command):
    """Run a read-only version command on the host OS over SSH."""
    if not host_ip or not command:
        return ""
    client, err = _ssh_connect(host_ip)
    if err:
        return ""
    try:
        _code, out, _e = _ssh_run(client, command)
        return out.splitlines()[0].strip() if out else ""
    finally:
        client.close()


def _read_bmc_version(bmc_ip):
    inventory, _err = _fetch_firmware_inventory(bmc_ip)
    if inventory.get("FW_BMC_0", {}).get("version"):
        return inventory["FW_BMC_0"]["version"]
    client, err = _ssh_connect(bmc_ip)
    if err:
        return ""
    try:
        _code, out, _e = _ssh_run(
            client, '. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-$VERSION}"'
        )
        return out.splitlines()[0].strip() if out else ""
    finally:
        client.close()


def _read_bmc_os_release(bmc_ip):
    """Run cat /etc/os-release on the BMC over SSH."""
    command = "cat /etc/os-release"
    client, err = _ssh_connect(bmc_ip)
    if err:
        return False, err, "", command, ""

    try:
        code, out, err_out = _ssh_run(client, command, timeout=SSH_CMD_TIMEOUT)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        ).strip()
        if code != 0 and not combined:
            return False, f"Command failed (exit {code}).", "", command, ""

        version_id = ""
        for line in combined.splitlines():
            if line.startswith("VERSION_ID="):
                version_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
            if not version_id and line.startswith("VERSION="):
                version_id = line.split("=", 1)[1].strip().strip('"').strip("'")

        if code != 0:
            return False, f"Command failed (exit {code}): {combined}", combined, command, version_id
        return True, combined or "No output.", combined, command, version_id
    finally:
        try:
            client.close()
        except Exception:
            pass


def _bundled_image_path(image_file):
    return os.path.join(FIRMWARE_IMAGES_DIR, image_file)


# ---------------------------------------------------------------------------
# Background flash jobs (survive tab switches; polled by the GUI side panel)
# ---------------------------------------------------------------------------
_FLASH_JOBS = {}
_FLASH_LOCK = threading.Lock()


def _job_create(bmc_ip, label, method, curl_command="", extra=None):
    job_id = str(uuid.uuid4())
    job = {
        "id": job_id,
        "bmc_ip": bmc_ip,
        "label": label,
        "method": method,
        "status": "queued",
        "percent": 0,
        "phase": "Queued",
        "message": "Waiting to start…",
        "curl_command": curl_command,
        "error": None,
        "terminal_log": [],
        "ssh_terminal_log": [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    if extra:
        job.update(extra)
    with _FLASH_LOCK:
        _FLASH_JOBS[job_id] = job
    return job_id


def _job_update(job_id, **fields):
    with _FLASH_LOCK:
        job = _FLASH_JOBS.get(job_id)
        if not job:
            return
        if "percent" in fields:
            fields["percent"] = max(0, min(100, int(fields["percent"])))
        job.update(fields)
        job["updated_at"] = time.time()


def _job_log(job_id, line, style="out", log_key="terminal_log"):
    """Append one line to a job terminal log (shown in the progress panel).

    log_key selects which pane: 'terminal_log' (Redfish/pldmd) or
    'ssh_terminal_log' (SSH activation after the update task finishes).
    """
    if not job_id or line is None:
        return
    if log_key not in ("terminal_log", "ssh_terminal_log"):
        log_key = "terminal_log"
    with _FLASH_LOCK:
        job = _FLASH_JOBS.get(job_id)
        if not job:
            return
        log = job.setdefault(log_key, [])
        log.append({"line": str(line), "style": style, "t": time.time()})
        if len(log) > 600:
            job[log_key] = log[-600:]
        job["updated_at"] = time.time()


def _job_ssh_log(job_id, line, style="out"):
    """Append to the SSH activation terminal for a flash job."""
    _job_log(job_id, line, style=style, log_key="ssh_terminal_log")


def _job_snapshot(job_id):
    with _FLASH_LOCK:
        job = _FLASH_JOBS.get(job_id)
        return dict(job) if job else None


def _jobs_for_bmc(bmc_ip):
    with _FLASH_LOCK:
        return [dict(j) for j in _FLASH_JOBS.values() if j.get("bmc_ip") == bmc_ip]


def _enqueue_flash(bmc_ip, label, method, curl_command, worker, cleanup_path=None, extra=None):
    """Run a flash worker in a background thread; return job_id immediately."""
    job_id = _job_create(bmc_ip, label, method, curl_command, extra)

    def _run():
        try:
            _job_update(
                job_id,
                status="running",
                percent=1,
                phase="Starting",
                message="Flash job started",
            )
            ok, message = worker(job_id)
            if ok:
                _job_update(
                    job_id,
                    status="completed",
                    percent=100,
                    phase="Complete",
                    message=message or "Flash completed successfully.",
                )
            else:
                snap = _job_snapshot(job_id) or {}
                _job_update(
                    job_id,
                    status="failed",
                    percent=min(snap.get("percent", 0), 99),
                    phase="Failed",
                    error=message,
                    message=message or "Flash failed.",
                )
        except Exception as exc:  # noqa: BLE001
            _job_update(
                job_id,
                status="failed",
                phase="Failed",
                error=str(exc),
                message=str(exc),
            )
        finally:
            if cleanup_path and os.path.exists(cleanup_path):
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass

    threading.Thread(target=_run, daemon=True).start()
    return job_id


# ---------------------------------------------------------------------------
# Flash upload staging (zip / folder → pick which .fwpkg to flash)
# ---------------------------------------------------------------------------
FLASH_IMAGE_EXTENSIONS = {".fwpkg", ".image", ".img", ".bin", ".tar"}
_FLASH_BUNDLES = {}
_FLASH_BUNDLE_LOCK = threading.Lock()
_FLASH_BUNDLE_TTL = 3600


def _safe_upload_relpath(rel):
    """Sanitize a browser-relative path (folder uploads) for disk storage."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    parts = []
    for part in rel.split("/"):
        if not part or part in (".", ".."):
            continue
        parts.append(part)
    return "/".join(parts)


def _cleanup_flash_bundles():
    """Drop staged upload bundles older than the TTL."""
    cutoff = time.time() - _FLASH_BUNDLE_TTL
    with _FLASH_BUNDLE_LOCK:
        expired = [bid for bid, b in _FLASH_BUNDLES.items() if b.get("created", 0) < cutoff]
        for bid in expired:
            bundle = _FLASH_BUNDLES.pop(bid, None)
            if bundle and bundle.get("dir"):
                shutil.rmtree(bundle["dir"], ignore_errors=True)


def _scan_flash_candidates(root_dir):
    """Recursively list flashable images under a staged upload directory."""
    out = []
    for dirpath, _dirs, files in os.walk(root_dir):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in FLASH_IMAGE_EXTENSIONS:
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root_dir).replace("\\", "/")
            out.append({
                "id": hashlib.md5(rel.encode("utf-8")).hexdigest()[:12],
                "name": name,
                "path": rel,
                "size": os.path.getsize(full),
                "ext": ext,
            })
    return out


def _extract_nested_zips(root_dir, max_depth=3):
    """Unzip any .zip files found under root_dir (e.g. Quanta BMC/v1.09.00.zip)."""
    for _ in range(max_depth):
        found = False
        for dirpath, _dirs, files in os.walk(root_dir):
            for name in list(files):
                if not name.lower().endswith(".zip"):
                    continue
                found = True
                zpath = os.path.join(dirpath, name)
                extract_to = os.path.join(dirpath, os.path.splitext(name)[0])
                os.makedirs(extract_to, exist_ok=True)
                try:
                    with zipfile.ZipFile(zpath, "r") as zf:
                        zf.extractall(extract_to)
                    os.unlink(zpath)
                except (zipfile.BadZipFile, OSError):
                    pass
        if not found:
            break


def _bundle_file_path(bundle_id, file_id, bmc_ip):
    """Return (absolute_path, display_name) for a staged file, or (None, None)."""
    _cleanup_flash_bundles()
    with _FLASH_BUNDLE_LOCK:
        bundle = _FLASH_BUNDLES.get(bundle_id)
        if not bundle or bundle.get("bmc_ip") != bmc_ip:
            return None, None
        entry = next((f for f in bundle.get("files", []) if f["id"] == file_id), None)
        if not entry:
            return None, None
        full = os.path.join(bundle["dir"], entry["path"].replace("/", os.sep))
        if not os.path.isfile(full):
            return None, None
        return full, entry.get("name") or os.path.basename(full)


def _image_from_flash_request(bmc_ip):
    """Resolve uploaded image: direct file or staged bundle selection.

    Returns (image_path, cleanup_path, orig_name, error_response).
    """
    bundle_id = (request.form.get("bundle_id") or "").strip()
    file_id = (request.form.get("file_id") or "").strip()
    if bundle_id and file_id:
        src_path, orig_name = _bundle_file_path(bundle_id, file_id, bmc_ip)
        if not src_path:
            return None, None, None, (jsonify({"error": "Invalid bundle or file selection."}), 400)
        suffix = os.path.splitext(orig_name)[1] or ".bin"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.close()
        shutil.copy2(src_path, tmp.name)
        return tmp.name, tmp.name, orig_name, None

    upload = request.files.get("image")
    if upload and upload.filename:
        orig_name = os.path.basename(upload.filename)
        suffix = os.path.splitext(orig_name)[1] or ".image"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        upload.save(tmp.name)
        tmp.close()
        return tmp.name, tmp.name, orig_name, None

    return None, None, None, (jsonify({"error": "Choose a firmware file to flash."}), 400)


def _redfish_curl_hint(bmc_ip, image_path, targets=None):
    """Equivalent curl command shown in the progress panel (matches NVIDIA doc)."""
    update_params = {"ForceUpdate": True}
    if targets:
        update_params["Targets"] = targets
    params = json.dumps(update_params)
    img = os.path.basename(image_path)
    return (
        f"curl -sku $BMC_USER:$BMC_PASS \\\n"
        f'  -X POST https://{bmc_ip}/redfish/v1/UpdateService/update-multipart \\\n'
        f"  -F 'UpdateParameters={params};type=application/json' \\\n"
        f"  -F UpdateFile=@{img}\n"
        f"\n# Monitor on the BMC:\n"
        f"ssh $BMC_USER@{bmc_ip} journalctl -u pldmd -f"
    )


def _initramfs_curl_hint(bmc_ip, image_path):
    img = os.path.basename(image_path)
    return (
        f"scp {img} $BMC_USER@{bmc_ip}:/run/initramfs/image-bmc\n"
        f"ssh $BMC_USER@{bmc_ip} md5sum /run/initramfs/image-bmc\n"
        f"ssh $BMC_USER@{bmc_ip} /run/initramfs/update"
    )


class _MultipartUpload:
    """Streaming multipart/form-data body for Redfish update-multipart.

    requests' files= helper buffers the whole image in memory and hands the
    socket one very large TLS write; on slow BMC links that surfaces as
    SSLWantWriteError. Streaming keeps each socket write small and memory flat,
    and still reports upload progress to the flash job.
    """

    def __init__(self, path, params, job_id=None, start_pct=5, end_pct=48,
                 phase="Uploading via Redfish"):
        self.boundary = f"mgxarc{uuid.uuid4().hex}"
        filename = os.path.basename(path)
        self._head = (
            f"--{self.boundary}\r\n"
            'Content-Disposition: form-data; name="UpdateParameters"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{params}\r\n"
            f"--{self.boundary}\r\n"
            'Content-Disposition: form-data; name="UpdateFile"; '
            f'filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        self._tail = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self._size = os.path.getsize(path)
        self._fh = open(path, "rb")
        self._head_pos = 0
        self._tail_pos = 0
        self._sent = 0
        self._file_done = False
        self._last_mb = -1
        self.job_id = job_id
        self.start_pct = start_pct
        self.end_pct = end_pct
        self.phase = phase

    @property
    def content_type(self):
        return f"multipart/form-data; boundary={self.boundary}"

    def __len__(self):
        return len(self._head) + self._size + len(self._tail)

    def _report(self):
        mb = self._sent // (1024 * 1024)
        if not self.job_id or mb == self._last_mb:
            return
        self._last_mb = mb
        span = self.end_pct - self.start_pct
        pct = self.start_pct + int((self._sent / (self._size or 1)) * span)
        _job_update(
            self.job_id,
            percent=pct,
            phase=self.phase,
            message=f"Uploading image… {mb}/{self._size // (1024 * 1024)} MB",
        )

    def read(self, amt=-1):
        remaining = None if amt is None or amt < 0 else amt
        chunks = []
        while remaining is None or remaining > 0:
            if self._head_pos < len(self._head):
                end = (
                    len(self._head) if remaining is None
                    else min(len(self._head), self._head_pos + remaining)
                )
                part = self._head[self._head_pos:end]
                self._head_pos = end
            elif not self._file_done:
                part = self._fh.read(-1 if remaining is None else remaining)
                if not part:
                    self._file_done = True
                    continue
                self._sent += len(part)
                self._report()
            elif self._tail_pos < len(self._tail):
                end = (
                    len(self._tail) if remaining is None
                    else min(len(self._tail), self._tail_pos + remaining)
                )
                part = self._tail[self._tail_pos:end]
                self._tail_pos = end
            else:
                break
            chunks.append(part)
            if remaining is not None:
                remaining -= len(part)
        return b"".join(chunks)

    def close(self):
        self._fh.close()


def _task_path_from_response(resp):
    """Extract the Redfish Task path from an update-multipart POST response."""
    # OpenBMC returns 202 with the Task resource as the body and a Location header.
    try:
        body = resp.json()
    except ValueError:
        body = {}
    odata = body.get("@odata.id") if isinstance(body, dict) else None
    if odata and "/TaskService/Tasks/" in odata:
        return odata.split("/redfish/v1", 1)[-1], body
    loc = resp.headers.get("Location", "")
    if "/TaskService/Tasks/" in loc:
        # Location may include a TaskMonitor suffix; keep the task path.
        path = loc.split("/redfish/v1", 1)[-1]
        return path, body
    return None, body


def _redfish_error_message(resp):
    """Return a human error string if a Redfish response body is an error.

    Handles the '{"error": {...}}' envelope BMCs return (e.g. Base.InternalError
    when the uploaded file is missing/not a real .fwpkg). Returns "" otherwise.
    """
    try:
        body = resp.json()
    except ValueError:
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if not isinstance(err, dict):
        return ""
    msg = err.get("message") or ""
    ext = err.get("@Message.ExtendedInfo")
    if isinstance(ext, list) and ext:
        first = ext[0]
        if isinstance(first, dict) and first.get("Message"):
            msg = first.get("Message")
    return str(msg).strip()


def _messages_text(messages):
    """Flatten a Redfish Messages array to a readable string."""
    out = []
    for m in messages or []:
        if isinstance(m, dict):
            txt = m.get("Message") or m.get("MessageId") or ""
            if txt:
                out.append(str(txt))
    return " | ".join(out)


# Task-message markers that mean the update was genuinely rejected.
_HARD_ERROR_MARKERS = (
    "verificationfailed", "verification failed", "signature", "signing",
    "transferfailed", "transfer failed", "invalid image", "invalidimage",
    "incompatible", "unsupported", "internalerror", "internal error",
    "corrupt", "size mismatch", "not signed", "authentication",
    "writefailed", "write failed", "flashfailed", "flash failed",
)
# Markers that are non-fatal NVIDIA BMC noise during an otherwise-OK update.
_SOFT_ERROR_MARKERS = (
    "debug token", "debug_token", "debugtoken",
    "no matching devices", "nomatchingdevices",
    "erase failed for one or more devices",
    "erase a debug token",
)


def _classify_task_messages(messages):
    """Return ('ok'|'soft'|'hard', text) for a task's Messages array.

    NVIDIA MGX/HGX BMCs routinely emit debug-token-erase and 'No Matching
    Devices' errors during a successful firmware update. Those are treated as
    'soft' (warnings) so we fall back to verifying the real version, while true
    signature/transfer/verification failures are 'hard'.
    """
    text = _messages_text(messages)
    low = text.lower()
    if not low:
        return "ok", ""
    for marker in _HARD_ERROR_MARKERS:
        if marker in low:
            return "hard", text
    for marker in _SOFT_ERROR_MARKERS:
        if marker in low:
            return "soft", text
    # Generic error words only count if nothing else classified them.
    if any(w in low for w in ("failed", "error", "rejected", "denied", "unable")):
        return "soft", text
    return "ok", text


def _poll_redfish_task(bmc_ip, job_id, task_path, start_pct=48, end_pct=98, max_wait=1800):
    """Follow a Redfish Task to completion (mirrors polling GET on the Task).

    Returns (status, message) where status is one of:
      "ok"     – task completed cleanly
      "soft"   – task completed but with non-fatal warnings (verify version)
      "hard"   – task genuinely failed (do not claim success)
      "reboot" – BMC went away after starting (self-update reboot)
      "applied"– task reported 100% but never left Running (image is written,
                 the BMC still has to reboot/activate)

    Some MGX/Quanta BMCs report PercentComplete=0 for the whole "Running" phase
    and only jump to 100 at "Completed". To avoid a frozen bar we creep the
    displayed percent upward while the task is running, and always use the real
    percent when the BMC provides one.
    """
    deadline = time.time() + max_wait
    last_pct = start_pct
    unreachable = 0
    seen_running = False
    last_msg = ""
    hundred_since = None
    while time.time() < deadline:
        rel = task_path.split("/redfish/v1", 1)[-1]
        ok, task, _status = _redfish_get(bmc_ip, rel)
        if ok and isinstance(task, dict):
            unreachable = 0
            seen_running = True
            state = str(task.get("TaskState", "")).strip()
            try:
                pct = float(task.get("PercentComplete"))
            except (TypeError, ValueError):
                pct = None
            messages = task.get("Messages", [])
            msg_text = _messages_text(messages)
            if msg_text:
                last_msg = msg_text
            if pct is not None and pct > 0:
                # BMC reports real progress — trust it.
                total = start_pct + int(pct * (end_pct - start_pct) / 100)
                last_pct = max(last_pct, total)
            else:
                # BMC is stuck at 0 while working — creep the bar so the user
                # sees continuous movement until the task actually completes.
                last_pct = min(last_pct + 2, end_pct - 1)
            _job_update(
                job_id,
                percent=last_pct,
                phase=f"BMC applying update ({state or 'Running'})",
                message=msg_text or f"Task {state or 'running'}… {int(pct) if pct is not None else 0}%",
            )
            state_l = state.lower()
            # Success can be signalled by TaskState=Completed or by the
            # UpdateSuccessful / TaskCompletedOK messages this BMC emits.
            low_msg = msg_text.lower()
            success_markers = ("updatesuccessful", "successfully updated",
                               "taskcompletedok", "has completed")
            done_by_msg = any(m in low_msg for m in success_markers)
            if state_l in ("completed", "exception", "cancelled", "killed", "interrupted") or (
                done_by_msg and state_l not in ("running", "starting", "new", "pending")
            ):
                kind, detail = _classify_task_messages(messages)
                if state_l not in ("completed", "") and kind == "ok" and not done_by_msg:
                    # Non-completed state with no useful messages → treat as hard.
                    kind = "hard"
                return kind, detail or f"Update task {state or 'finished'}."

            # MGX ARC BMCs report PercentComplete=100 but leave TaskState at
            # "Running" because they reboot to activate, so the terminal state
            # never arrives. Once the write has been at 100% for a moment,
            # stop waiting and let the caller activate and verify.
            if (pct is not None and pct >= 100) or "100 percent complete" in low_msg:
                if hundred_since is None:
                    hundred_since = time.time()
                    _job_update(
                        job_id,
                        percent=last_pct,
                        phase="BMC applying update",
                        message="Image written (100%) — waiting for the BMC to activate…",
                    )
                elif time.time() - hundred_since >= TASK_ACTIVATION_GRACE:
                    kind, detail = _classify_task_messages(messages)
                    if kind == "hard":
                        return "hard", detail or "Update task reported an error at 100%."
                    return "applied", detail or last_msg or (
                        "Update reported 100% complete — the BMC still has to "
                        "reboot to activate the new firmware."
                    )
            else:
                hundred_since = None
        else:
            # BMC unreachable. Only meaningful if the task was already running
            # (BMC self-update reboots mid-flash). Otherwise keep waiting.
            unreachable += 1
            _job_update(
                job_id,
                percent=min(last_pct + 1, end_pct - 2),
                phase="BMC unreachable",
                message="BMC not responding — it may be rebooting to apply the update…",
            )
            if seen_running and unreachable >= 6:
                return "reboot", (
                    "BMC stopped responding after the update task started — it is likely "
                    "rebooting to apply firmware. Verify the version after it comes back."
                )
            if not seen_running and unreachable >= 20:
                return "hard", (
                    "Could not reach the update task on the BMC. The update may not have "
                    "started — verify the version and retry."
                )
        time.sleep(2)
    return "soft", (
        f"Update task did not complete within {max_wait}s. Last status: "
        f"{last_msg or 'unknown'}."
    )


def _task_sort_key(path):
    tail = path.rsplit("/", 1)[-1]
    try:
        return (0, int(tail))
    except ValueError:
        return (1, tail)


def _list_task_paths(bmc_ip):
    """Return every Redfish task path on the BMC, oldest first."""
    ok, data, _status = _redfish_get(bmc_ip, "/TaskService/Tasks")
    if not ok or not isinstance(data, dict):
        return []
    paths = []
    for m in data.get("Members", []):
        odata = m.get("@odata.id") if isinstance(m, dict) else None
        if odata and "/TaskService/Tasks/" in odata:
            paths.append(odata.split("/redfish/v1", 1)[-1])
    paths.sort(key=_task_sort_key)
    return paths


def _find_latest_update_task(bmc_ip):
    """Return the path of the most recent Redfish task, or None.

    Used as a fallback when the update-multipart POST times out or the
    connection drops: NVIDIA MGX/HGX BMCs frequently accept the image and
    start a task without ever returning the HTTP response over a slow link.
    We look up the newest task so we can still follow the real progress.
    """
    paths = _list_task_paths(bmc_ip)
    return paths[-1] if paths else None


def _task_percent(task):
    """PercentComplete as a float, or None when the BMC omits/garbles it."""
    try:
        return float(task.get("PercentComplete"))
    except (TypeError, ValueError):
        return None


def _active_update_tasks(bmc_ip):
    """Return [(path, task)] for tasks the BMC still considers unfinished."""
    active = []
    for path in _list_task_paths(bmc_ip):
        ok, task, _status = _redfish_get(bmc_ip, path)
        if not ok or not isinstance(task, dict):
            continue
        state = str(task.get("TaskState", "")).strip().lower()
        if state in ACTIVE_TASK_STATES:
            active.append((path, task))
    return active


def _clear_stale_update_tasks(bmc_ip, job_id=None):
    """Delete finished-but-still-'Running' tasks that block a new update.

    A task sitting at PercentComplete=100 while still 'Running' is one this BMC
    never closed out (it reboots to activate instead). It keeps UpdateService
    rejecting new uploads with 'An update is in progress', so remove it.

    Returns (busy, cleared) where busy is a (path, task) tuple for an update
    that is genuinely still progressing, and cleared is how many stale tasks
    were deleted.
    """
    cleared = 0
    for path, task in _active_update_tasks(bmc_ip):
        pct = _task_percent(task)
        state = str(task.get("TaskState", "")).strip() or "Running"
        if pct is not None and pct < 100:
            return (path, task), cleared
        _job_log(
            job_id,
            f"(clearing stale update task {path} — {state} at {int(pct) if pct is not None else '?'}%)",
            "info",
        )
        ok, _data, code = _redfish_delete(bmc_ip, path)
        if ok:
            cleared += 1
        else:
            _job_log(job_id, f"(could not delete {path}: HTTP {code})", "info")
    return None, cleared


def _release_update_service(bmc_ip, job_id=None):
    """Last resort for a wedged UpdateService: restart bmcweb over SSH.

    bmcweb holds an 'update in progress' flag for the lifetime of its update
    task. When a flash is interrupted the flag can outlive the task itself, and
    restarting bmcweb is the only way to release it without rebooting the BMC.
    """
    client, err = _ssh_connect(bmc_ip)
    if err:
        return False, f"SSH unavailable ({err})"
    try:
        _ssh_run_logged(client, "systemctl restart bmcweb", job_id=job_id, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    finally:
        try:
            client.close()
        except Exception:
            pass
    time.sleep(5)
    if not _wait_for_bmc_online(bmc_ip, job_id=job_id, timeout=180, poll_interval=5):
        return False, "bmcweb was restarted but Redfish did not come back."
    return True, ""


def _is_update_in_progress(resp):
    """True when a rejected update-multipart response means 'already updating'."""
    detail = ""
    try:
        detail = _redfish_error_message(resp) or ""
    except Exception:  # noqa: BLE001
        detail = ""
    text = f"{detail} {getattr(resp, 'text', '')[:500]}".lower()
    return any(marker in text for marker in UPDATE_IN_PROGRESS_MARKERS)


def _describe_busy_task(path, task):
    pct = _task_percent(task)
    state = str(task.get("TaskState", "")).strip() or "Running"
    task_id = path.rsplit("/", 1)[-1]
    return (
        f"The BMC is already applying an update (task {task_id}, {state}"
        + (f" at {int(pct)}%" if pct is not None else "")
        + "). Wait for it to finish, then flash again."
    )


def _preflight_update_service(bmc_ip, job_id=None):
    """Clear leftovers so UpdateService will accept a new image.

    Returns an error message when a real update is still running, else "".
    """
    busy, cleared = _clear_stale_update_tasks(bmc_ip, job_id)
    if busy:
        return _describe_busy_task(*busy)
    if cleared:
        # Deleting the task does not by itself release bmcweb's in-progress
        # flag, and finding out costs a full upload — so release it now.
        if job_id:
            _job_update(
                job_id,
                phase="Clearing previous update",
                message=f"Cleared {cleared} stale update task(s) from a previous flash.",
            )
        ok, err = _release_update_service(bmc_ip, job_id)
        if not ok:
            _job_log(job_id, f"(could not restart bmcweb: {err})", "info")
    return ""


def _recover_update_in_progress(bmc_ip, job_id=None):
    """Handle a 400 'An update is in progress' from update-multipart.

    Returns (ok_to_retry, message).
    """
    _job_log(job_id, "(BMC rejected the upload: an update is already in progress)", "info")
    if job_id:
        _job_update(
            job_id,
            phase="Clearing previous update",
            message="BMC still thinks an update is running — clearing it…",
        )

    busy, _cleared = _clear_stale_update_tasks(bmc_ip, job_id)
    if busy:
        return False, _describe_busy_task(*busy)

    # No task is actually running, so bmcweb is holding a stale in-progress
    # flag. Restarting it is the documented way to release that.
    ok, err = _release_update_service(bmc_ip, job_id)
    if not ok:
        return False, (
            "The BMC reports an update is in progress but no update task is running. "
            f"Could not clear it automatically ({err}). Run 'systemctl restart bmcweb' "
            "on the BMC, or power cycle it, then retry the flash."
        )
    if job_id:
        _job_update(
            job_id,
            phase="Retrying upload",
            message="UpdateService released — re-uploading the image…",
        )
    return True, ""


def _recover_via_task(bmc_ip, job_id, label, verify_id, verify_version, expect_reboot):
    """After an upload timeout/drop, try to find and follow the BMC's task.

    Returns (handled, ok, message). handled=False means no task was found and
    the caller should fall back to its own timeout messaging.
    """
    task_path = _find_latest_update_task(bmc_ip)
    if not task_path:
        return False, False, ""
    if job_id:
        _job_update(
            job_id,
            phase="Recovering update task",
            message="Upload response was lost — following the update task the BMC started…",
        )
        _job_log(
            job_id,
            f"(upload response lost; following {task_path} that the BMC started)",
            "info",
        )
    status, task_msg = _poll_redfish_task(bmc_ip, job_id, task_path)
    if status == "hard":
        return True, False, f"{label} update failed: {task_msg}"
    no_match = "no matching devices" in task_msg.lower()
    warn = f" Note: BMC reported non-fatal warnings ({task_msg})." if status == "soft" else ""
    if verify_id and verify_version:
        changed, cur = _verify_version_changed(bmc_ip, verify_id, verify_version, retries=6)
        if changed:
            return True, True, f"{label} updated to {cur}.{warn}".strip()
        if (expect_reboot or status == "applied") and not no_match:
            return True, True, (
                f"{label}: update task finished — reboot / cold power cycle to activate, then "
                f"the version will update.{warn}".strip()
            )
    if no_match:
        return True, False, (
            f"{label}: BMC reported 'No Matching Devices' — nothing was flashed. Confirm the "
            f"package matches this board, or use the documented .bin SCP method for the BMC."
        )
    if expect_reboot or status in ("reboot", "applied"):
        return True, True, (
            f"{label}: update task finished — the BMC/host must reboot (or be cold power "
            f"cycled) to activate the new firmware, then the version will update.{warn}".strip()
        )
    return True, True, f"{label} update completed.{warn}".strip()


def _flash_target(target_id):
    for target in BMC_FLASH_TARGETS:
        if target["id"] == target_id:
            return target
    return None


def _verify_version_changed(bmc_ip, verify_id, verify_version, retries=3, delay=4):
    """Re-read a component version and check it matches the target."""
    last = ""
    for _ in range(retries):
        last = _read_inventory_version(bmc_ip, verify_id)
        if last and _normalize_fw_version(last) == _normalize_fw_version(verify_version):
            return True, last
        time.sleep(delay)
    return False, last


class _PldmdMonitor:
    """Stream `journalctl -u pldmd -f` from the BMC into a job's terminal log.

    Mirrors the documented monitoring step so the fwpkg update is visible live.
    Runs on its own SSH connection in a background thread and is best-effort.
    """

    def __init__(self, bmc_ip, job_id):
        self.bmc_ip = bmc_ip
        self.job_id = job_id
        self._stop = threading.Event()
        self._thread = None
        self._client = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        client, err = _ssh_connect(self.bmc_ip)
        if err:
            _job_log(self.job_id, f"(pldmd monitor unavailable: {err})", "info")
            return
        self._client = client
        try:
            _job_log(self.job_id, "$ journalctl -u pldmd -f", "cmd")
            _stdin, stdout, _stderr = client.exec_command(
                "journalctl -u pldmd -f -n 0 --no-pager", timeout=None
            )
            channel = stdout.channel
            while not self._stop.is_set():
                if channel.recv_ready():
                    chunk = stdout.read(4096).decode("utf-8", "replace")
                    for line in chunk.splitlines():
                        line = line.rstrip("\r")
                        if line:
                            _job_log(self.job_id, line, "out")
                elif channel.exit_status_ready():
                    break
                else:
                    time.sleep(0.2)
        except Exception:  # noqa: BLE001 - monitoring is best-effort
            pass
        finally:
            try:
                client.close()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass


def _flash_redfish_multipart(
    bmc_ip, image_path, targets=None, label="Firmware", job_id=None,
    verify_id=None, verify_version=None, expect_reboot=False,
):
    """Wrapper: run the Redfish multipart update while mirroring the documented
    `journalctl -u pldmd -f` monitor into the job's live terminal."""
    pldmd = None
    if job_id:
        _job_log(job_id, f"=== {label} update (fwpkg via Redfish update-multipart) ===", "info")
        _job_log(job_id, f"Target BMC: {bmc_ip}", "info")
        _job_log(job_id, f"Image: {os.path.basename(image_path)}", "info")
        pldmd = _PldmdMonitor(bmc_ip, job_id)
        pldmd.start()
    try:
        return _redfish_multipart_run(
            bmc_ip, image_path, targets=targets, label=label, job_id=job_id,
            verify_id=verify_id, verify_version=verify_version,
            expect_reboot=expect_reboot,
        )
    finally:
        if pldmd:
            # Give pldmd a moment to flush final lines, then stop the monitor.
            time.sleep(1.0)
            pldmd.stop()


def _redfish_multipart_run(
    bmc_ip, image_path, targets=None, label="Firmware", job_id=None,
    verify_id=None, verify_version=None, expect_reboot=False,
):
    """Redfish update-multipart — equivalent to curl -F UpdateParameters -F UpdateFile.

    Follows the Redfish Task returned by the BMC and only reports success when the
    task actually completes without errors. Where possible it also re-reads the
    component version to confirm the firmware really changed.
    """
    url = f"{_bmc_base(bmc_ip)}/UpdateService/update-multipart"
    # Match the documented curl exactly: send {"ForceUpdate":true} with NO
    # Targets field when targeting nothing specific (the package's own
    # descriptors are used). Only include Targets when explicitly provided.
    update_params = {"ForceUpdate": True}
    if targets:
        update_params["Targets"] = targets
    params = json.dumps(update_params)

    if not os.path.isfile(image_path):
        return False, f"Firmware image not found: {image_path}"

    if job_id:
        _job_update(
            job_id,
            percent=3,
            phase="Preparing Redfish upload",
            message="Opening connection to UpdateService/update-multipart",
        )

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    def _upload_once():
        """POST the image once.

        Returns (resp, early) — early is a (ok, message) result to return
        straight away, and is None when resp should be inspected instead.
        """
        reader = None
        try:
            reader = _MultipartUpload(
                image_path, params, job_id, 5, 48, phase="Uploading via Redfish"
            )
            try:
                return session.post(
                    url,
                    auth=_bmc_auth(),
                    data=reader,
                    verify=False,
                    timeout=(UPLOAD_CONNECT_TIMEOUT, UPLOAD_TIMEOUT),
                    headers={"Expect": "", "Content-Type": reader.content_type},
                ), None
            except requests.exceptions.Timeout:
                # Upload stalled waiting for the response. On NVIDIA BMCs the
                # task usually started anyway — look it up and follow it.
                handled, ok, msg = _recover_via_task(
                    bmc_ip, job_id, label, verify_id, verify_version, expect_reboot
                )
                if handled:
                    return None, (ok, msg)
                if verify_id and verify_version:
                    changed, cur = _verify_version_changed(bmc_ip, verify_id, verify_version)
                    if changed:
                        return None, (
                            True,
                            f"{label} updated to {cur} (upload timed out but version confirmed).",
                        )
                if expect_reboot:
                    return None, (True, (
                        f"{label}: upload response timed out but the BMC may still be applying "
                        "the update and rebooting. Verify the version after it comes back."
                    ))
                return None, (False, (
                    f"Upload to the BMC timed out after {UPLOAD_TIMEOUT}s and no update task was "
                    "found. Retry, or use a wired/closer network to the BMC."
                ))
            except requests.exceptions.ConnectionError as exc:
                inner = str(exc).lower()
                if any(marker in inner for marker in UPLOAD_INTERRUPTED_MARKERS):
                    handled, ok, msg = _recover_via_task(
                        bmc_ip, job_id, label, verify_id, verify_version, expect_reboot
                    )
                    if handled:
                        return None, (ok, msg)
                    if expect_reboot:
                        return None, (True, (
                            f"{label}: connection dropped during upload — the BMC may be applying "
                            "the update and rebooting. Verify the version after it comes back."
                        ))
                    if verify_id and verify_version:
                        changed, cur = _verify_version_changed(bmc_ip, verify_id, verify_version)
                        if changed:
                            return None, (
                                True,
                                f"{label} updated to {cur} (connection dropped but version confirmed).",
                            )
                    return None, (False, (
                        "The connection dropped while uploading and the version did not change. "
                        "Wait a few minutes, verify the version, and retry if needed."
                    ))
                return None, (False, f"Redfish update failed: {exc}")
            except requests.exceptions.RequestException as exc:
                return None, (False, f"Redfish update failed: {exc}")
        finally:
            if reader:
                reader.close()

    try:
        # A task left behind by an interrupted flash keeps UpdateService
        # rejecting new images, so clear it before spending an upload on it.
        busy = _preflight_update_service(bmc_ip, job_id)
        if busy:
            return False, busy

        resp, early = _upload_once()
        if early:
            return early

        if resp.status_code >= 400 and _is_update_in_progress(resp):
            ok, note = _recover_update_in_progress(bmc_ip, job_id)
            if not ok:
                return False, note
            resp, early = _upload_once()
            if early:
                return early
    finally:
        session.close()

    if resp.status_code >= 400:
        detail = _redfish_error_message(resp) or resp.text[:500]
        return False, f"BMC returned HTTP {resp.status_code}. {detail}".strip()

    # Some BMCs return HTTP 200 with an error body (e.g. InternalError) instead
    # of a task when the request/package is bad. Surface that instead of
    # pretending an update started.
    err_detail = _redfish_error_message(resp)
    if err_detail:
        return False, (
            f"{label}: the BMC rejected the update — {err_detail} "
            "Check that the file is a valid .fwpkg for this board and retry."
        )

    # Follow the Redfish Task returned by update-multipart.
    task_path, body = _task_path_from_response(resp)
    if job_id:
        _job_update(
            job_id,
            percent=50,
            phase="BMC applying update",
            message="Upload accepted — following update task on BMC",
        )

    if not task_path:
        # No task reference (older BMC). Fall back to version verification.
        if verify_id and verify_version:
            changed, cur = _verify_version_changed(bmc_ip, verify_id, verify_version, retries=6)
            if changed:
                return True, f"{label} updated to {cur}."
            return False, (
                f"{label}: BMC accepted the upload (HTTP {resp.status_code}) but the version is "
                f"still {cur or 'unchanged'}. The update may have been rejected — check the BMC "
                "event log."
            )
        if expect_reboot:
            return True, f"{label}: upload accepted. BMC should reboot to apply — verify version after."
        return False, (
            f"{label}: BMC accepted the upload but returned no task to track and the version "
            "could not be verified. Check the BMC event log."
        )

    status, task_msg = _poll_redfish_task(bmc_ip, job_id, task_path)

    # A genuine (hard) failure — never claim success.
    if status == "hard":
        return False, f"{label} update failed: {task_msg}"

    # "No Matching Devices" means the package applied to nothing — the image is
    # not flashable to this device via Redfish (common for the BMC image, which
    # must go through the .bin SCP → /run/initramfs/update method).
    no_match = "no matching devices" in task_msg.lower()

    warn = ""
    if status == "soft":
        warn = f" Note: BMC reported non-fatal warnings ({task_msg})."

    # Confirm the firmware actually changed where we can (source of truth).
    if verify_id and verify_version:
        if job_id:
            _job_update(job_id, percent=99, phase="Verifying", message="Confirming new version…")
        changed, cur = _verify_version_changed(bmc_ip, verify_id, verify_version, retries=6)
        if changed:
            return True, f"{label} updated to {cur}.{warn}".strip()
        if (expect_reboot or status == "applied") and not no_match:
            return True, (
                f"{label}: update task finished — reboot / cold power cycle to activate, then the "
                f"version will update.{warn}".strip()
            )
        if no_match:
            return False, (
                f"{label}: BMC reported 'No Matching Devices' — this image was not applied to any "
                f"device via Redfish. For the BMC image use the .bin (SCP → /run/initramfs/update) "
                f"method; otherwise confirm you selected the correct package for this board."
            )
        return False, (
            f"{label}: the task finished but the version is still {cur or 'unchanged'}. "
            f"The image may have been rejected. BMC said: {task_msg}"
        )

    # No way to verify a specific version (e.g. FML bundle / BMC w/o target id).
    if no_match:
        return False, (
            f"{label}: BMC reported 'No Matching Devices' — nothing was flashed. Confirm the "
            f"package matches this board, or use the documented .bin SCP method for the BMC."
        )
    if expect_reboot or status in ("reboot", "applied"):
        return True, (
            f"{label}: update task finished — the BMC/host must reboot (or be cold power "
            f"cycled) to activate the new firmware, then the version will update.{warn}".strip()
        )

    return True, f"{label} update completed.{warn}".strip()


def _normalize_fw_version(version):
    """Loose version compare (underscores vs hyphens, case)."""
    return (version or "").strip().lower().replace("_", "-")


def _read_inventory_version(bmc_ip, redfish_id):
    inventory, _err = _fetch_firmware_inventory(bmc_ip)
    return inventory.get(redfish_id, {}).get("version", "")


def _flash_inventory_target(bmc_ip, image_path, target_cfg, job_id=None):
    return _flash_redfish_multipart(
        bmc_ip,
        image_path,
        targets=[target_cfg["redfish_target"]],
        label=target_cfg.get("flash_label", target_cfg["label"]),
        job_id=job_id,
        verify_id=target_cfg.get("redfish_id"),
        verify_version=target_cfg.get("version"),
    )


def _wait_for_bmc_online(bmc_ip, job_id=None, timeout=600, poll_interval=10):
    """Wait until the BMC answers Redfish or SSH again after a power cycle."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _data, _status = _redfish_get(bmc_ip, "/UpdateService/FirmwareInventory/FW_BMC_0")
        if ok:
            return True
        client, err = _ssh_connect(bmc_ip)
        if not err:
            try:
                client.close()
            except Exception:
                pass
            return True
        if job_id:
            remaining = int(deadline - time.time())
            _job_update(
                job_id,
                phase="Waiting for BMC",
                message=f"BMC not responding yet — waiting after power cycle ({remaining}s left)…",
            )
        time.sleep(poll_interval)
    return False


def _wait_for_bmc_offline(bmc_ip, job_id=None, timeout=180, poll_interval=5):
    """Wait until the BMC stops answering Redfish (it started rebooting).

    Returns True if it went away, False if it stayed up for the whole timeout —
    either way the caller continues; this only avoids reading the old version
    from a BMC that has not begun its activation reboot yet.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        ok, _data, _status = _redfish_get(bmc_ip, "/UpdateService/FirmwareInventory/FW_BMC_0")
        if not ok:
            return True
        if job_id:
            _job_update(
                job_id,
                phase="Waiting for BMC",
                message="BMC still up — waiting for it to reboot into the new firmware…",
            )
        time.sleep(poll_interval)
    return False


def _bmc_aux_cycle(client, job_id=None, log_key="terminal_log"):
    """Run the documented standby power cycle. Returns (ok, detail).

    A real aux cycle cuts standby power and kills this SSH channel, so the
    command normally never gets to report an exit status. We append an rc
    marker: if it comes back the script actually ran and its code is
    meaningful, and if it does not, the connection died — which is what a
    working power cycle looks like.
    """
    code, out, err_out = _ssh_run_logged(
        client,
        "stbypowerctrl.sh aux_cycle 2>&1; echo rc=$?",
        job_id=job_id,
        timeout=120,
        log_key=log_key,
    )
    text = f"{out or ''}\n{err_out or ''}".strip()
    match = re.search(r"rc=(\d+)", text)
    if match is None:
        return True, "connection dropped during aux_cycle (expected — standby power cut)"
    rc = int(match.group(1))
    if rc == 0:
        return True, ""
    return False, f"exit {rc}" + (f": {text}" if text else " with no output")


def _bmc_activation_diagnostics(client, job_id=None):
    """Log why the power-cycle helper refused, so the cause is visible."""
    for cmd in (
        "command -v stbypowerctrl.sh || echo 'stbypowerctrl.sh not found in PATH'",
        "stbypowerctrl.sh 2>&1 | head -n 20",
    ):
        try:
            _ssh_run_logged(client, cmd, job_id=job_id, timeout=30)
        except Exception as exc:  # noqa: BLE001
            _job_log(job_id, f"({cmd} failed: {exc})", "info")


def _bmc_activation_fallback(bmc_ip, client, job_id=None):
    """Try to activate the new image when the aux cycle could not be run.

    A BMC reboot swaps to the newly written flash slot on its own. It is a
    weaker activation than a standby cycle — an ERoT-attested image may still
    need a real aux cycle — but it is far better than leaving the old firmware
    running with no attempt made.
    """
    _job_update(
        job_id,
        phase="Activating",
        message="Standby cycle unavailable — rebooting the BMC to activate…",
    )
    ok, _data, code = _redfish_post(
        bmc_ip, "/Managers/bmc/Actions/Manager.Reset", {"ResetType": "ForceRestart"}
    )
    if ok:
        _job_log(job_id, "(sent Redfish Manager.Reset ForceRestart)", "info")
        return True
    _job_log(job_id, f"(Manager.Reset unavailable: HTTP {code}) — falling back to reboot", "info")
    try:
        _ssh_run_logged(client, "nohup reboot >/dev/null 2>&1 &", job_id=job_id, timeout=30)
        return True
    except Exception as exc:  # noqa: BLE001
        _job_log(job_id, f"(reboot failed: {exc})", "err")
        return False


def _bmc_post_flash_activate(bmc_ip, job_id, from_version=""):
    """After a successful BMC flash: aux power cycle, wait, re-read version."""
    _job_update(
        job_id,
        percent=98,
        phase="Power cycling",
        message="Running stbypowerctrl.sh aux_cycle on the BMC…",
    )
    activation_error = ""
    client, ssh_err = _ssh_connect(bmc_ip)
    if ssh_err:
        # The BMC reboots itself to activate the new image, so SSH being down
        # right after the flash usually means activation is already under way.
        # Keep going and verify the version instead of giving up here.
        _job_log(
            job_id,
            f"(SSH unavailable: {ssh_err} — BMC is likely already rebooting to activate)",
            "info",
        )
    else:
        try:
            cycled, detail = _bmc_aux_cycle(client, job_id)
            if not cycled:
                activation_error = f"stbypowerctrl.sh aux_cycle failed ({detail})"
                _job_log(job_id, f"aux_cycle failed: {detail}", "err")
                _bmc_activation_diagnostics(client, job_id)
                if _bmc_activation_fallback(bmc_ip, client, job_id):
                    activation_error += "; fell back to a BMC reboot"
                else:
                    activation_error += " and the BMC could not be rebooted either"
        except Exception as exc:  # noqa: BLE001
            activation_error = f"power cycle could not be run ({exc})"
            _job_log(job_id, f"aux_cycle error: {exc}", "err")
        finally:
            try:
                client.close()
            except Exception:
                pass

    _job_update(
        job_id,
        percent=99,
        phase="Waiting for BMC",
        message="Waiting for the BMC to reboot and come back online…",
    )

    # Let the BMC actually drop before we start looking for it, otherwise we
    # "find" the still-running old firmware and read the old version.
    _wait_for_bmc_offline(bmc_ip, job_id=job_id)

    if not _wait_for_bmc_online(bmc_ip, job_id=job_id, timeout=900):
        return True, (
            "The BMC has not come back within 15 minutes. Check the system and "
            "verify the BMC version when it returns."
        )

    new_ver = ""
    for _attempt in range(30):
        new_ver = _read_bmc_version(bmc_ip)
        if new_ver and (not from_version or new_ver != from_version):
            break
        if job_id:
            _job_update(
                job_id,
                message=f"BMC online — reading firmware version… ({new_ver or 'pending'})",
            )
        time.sleep(10)

    if job_id:
        _job_update(job_id, to_version=new_ver or None)

    if new_ver:
        if from_version and new_ver == from_version:
            if activation_error:
                return False, (
                    f"The image was written but never activated: {activation_error}. The BMC "
                    f"still reports {new_ver}. Cold power cycle the tray (or run "
                    "'stbypowerctrl.sh aux_cycle' on the BMC yourself) and then use Verify "
                    "BMC Version."
                )
            return False, (
                f"The BMC rebooted but still reports {new_ver} — the new image was not "
                "activated. Re-flash, or cold power cycle the tray and check Verify BMC "
                "Version. (If you intentionally flashed this same version, it is already "
                "running.)"
            )
        return True, (
            f"BMC flash complete. Version is now {new_ver}"
            + (f" (was {from_version})." if from_version else ".")
        )

    return True, (
        "Flash and power cycle complete. BMC is back online but the version "
        "could not be read yet — use Verify BMC Version at the bottom of the Flash tab "
        "(after Power Cycle or Power Off + Power On on the Power tab)."
    )


def _flash_bmc_gui(bmc_ip, image_path, job_id, initramfs=False, from_version=""):
    """Flash BMC then aux power-cycle and verify the new version."""
    if initramfs:
        ok, msg = _flash_bmc_no_erot(bmc_ip, image_path, job_id=job_id)
    else:
        ok, msg = _flash_bmc_erot(
            bmc_ip, image_path, job_id=job_id, targets=[], verify_version=None,
        )
    if not ok:
        return False, msg
    return _bmc_post_flash_activate(bmc_ip, job_id, from_version=from_version)


def _flash_bmc_erot(bmc_ip, image_path, job_id=None, targets=None, verify_version=None):
    """BMC via Redfish update-multipart (BMC reboots to activate).

    Passing an explicit target (e.g. FW_BMC_0) routes the image straight to the
    BMC updater instead of relying on the package auto-match, which avoids the
    'No Matching Devices' response on some MGX ARC BMCs.
    """
    return _flash_redfish_multipart(
        bmc_ip, image_path, targets=targets or [], label="BMC", job_id=job_id,
        expect_reboot=True,
        verify_id="FW_BMC_0" if verify_version else None,
        verify_version=verify_version,
    )


def _flash_smr(bmc_ip, image_path, job_id=None):
    """FPGA / SMR: Redfish update-multipart targeting FW_FPGA_0."""
    return _flash_inventory_target(bmc_ip, image_path, SMR_FLASH_TARGET, job_id=job_id)


def _flash_erot(bmc_ip, image_path, job_id=None):
    """ERoT: Redfish update-multipart targeting FW_ERoT_CPU_0."""
    return _flash_inventory_target(bmc_ip, image_path, EROT_FLASH_TARGET, job_id=job_id)


def _flash_sbios(bmc_ip, image_path, job_id=None):
    """SBIOS / UEFI: Redfish update-multipart targeting FW_CPU_0.

    Naming the inventory item explicitly is what the working manual curl does.
    Leaving Targets out relies on the package descriptors auto-matching, which
    this BMC does not always do for SBIOS packages.

    No version verification happens here: the task only stages the image and
    finishes with 'AwaitToActivate', so FW_CPU_0 keeps reporting the old
    version until the power cycles in _sbios_post_flash_activate.
    """
    return _flash_redfish_multipart(
        bmc_ip,
        image_path,
        targets=[SBIOS_FLASH_TARGET["redfish_target"]],
        label="SBIOS",
        job_id=job_id,
    )


def _flash_sbios_gui(bmc_ip, image_path, job_id, from_version="", target_version=""):
    """Flash SBIOS via Redfish, then activate with aux_cycle and verify all versions."""
    ok, msg = _flash_sbios(bmc_ip, image_path, job_id=job_id)
    if not ok:
        return False, msg
    if not target_version:
        target_version = _sbios_target_from_filename(os.path.basename(image_path))
    return _sbios_post_flash_activate(
        bmc_ip, job_id, from_version=from_version, target_version=target_version,
    )


def _read_smr_version(bmc_ip):
    return _read_inventory_version(bmc_ip, SMR_FLASH_TARGET["redfish_id"])


def _read_erot_version(bmc_ip):
    return _read_inventory_version(bmc_ip, EROT_FLASH_TARGET["redfish_id"])


def _read_sbios_version(bmc_ip):
    return _read_inventory_version(bmc_ip, SBIOS_FLASH_TARGET["redfish_id"])


SBIOS_VERIFY_CMD = (
    "curl -sku root:0penBmc "
    "https://localhost/redfish/v1/UpdateService/FirmwareInventory/FW_CPU_0 2>/dev/null"
)


def _parse_redfish_version_text(text):
    """Extract Version from Redfish JSON (or a grep-style fragment)."""
    if not text:
        return ""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return (data.get("Version") or "").strip()
    except json.JSONDecodeError:
        pass
    match = re.search(r'"Version"\s*:\s*"([^"]+)"', text)
    return match.group(1).strip() if match else ""


def _read_sbios_version_via_bmc_ssh(bmc_ip, job_id=None):
    """Read SBIOS version from the BMC over SSH (FW_CPU_0 Redfish inventory)."""
    client, err = _ssh_connect(bmc_ip)
    if err:
        if job_id:
            _job_log(job_id, f"(SSH unavailable: {err})", "err")
        return ""
    try:
        if job_id:
            _job_log(job_id, f"$ {SBIOS_VERIFY_CMD}", "cmd")
        code, out, err_out = _ssh_run(client, SBIOS_VERIFY_CMD, timeout=SSH_CMD_TIMEOUT)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        ).strip()
        if job_id and combined:
            for line in combined.splitlines():
                _job_log(job_id, line, "out")
        if code == 0 and combined:
            ver = _parse_redfish_version_text(combined)
            if ver:
                return ver
    finally:
        try:
            client.close()
        except Exception:
            pass
    return _read_sbios_version(bmc_ip)


def _read_sbios_slots(bmc_ip):
    """Return (version, active_slot_version, inactive_slot_version) for FW_CPU_0.

    The BMC stages a new SBIOS into the slots first. The top-level Version only
    flips after stbypowerctrl.sh aux_cycle. Flash is not complete until all three
    report the target version, e.g.:

      "Version": "02.07.02"          # ActiveFirmwareSlot
      "Version": "02.07.02"          # InactiveFirmwareSlot
      "Version": "02.07.02"          # top-level Version

    This state is still incomplete (needs aux_cycle):

      "Version": "02.07.02"
      "Version": "02.07.02"
      "Version": "02.06.06"
    """
    ok, data, _status = _redfish_get(bmc_ip, "/UpdateService/FirmwareInventory/FW_CPU_0")
    if not ok or not isinstance(data, dict):
        return "", "", ""
    nvidia = (data.get("Oem") or {}).get("Nvidia") or {}
    active = (nvidia.get("ActiveFirmwareSlot") or {}).get("Version") or ""
    inactive = (nvidia.get("InactiveFirmwareSlot") or {}).get("Version") or ""
    return (data.get("Version") or "").strip(), active.strip(), inactive.strip()


def _sbios_target_from_filename(name):
    """Pull X.Y.Z from a typical SBIOS package name, e.g. ..._02.07.02_rel_prod.fwpkg."""
    if not name:
        return ""
    match = re.search(r"(\d+\.\d+\.\d+)(?:[_-](?:rel|dev|prod))", name, re.I)
    if match:
        return match.group(1)
    matches = re.findall(r"\d+\.\d+\.\d+", name)
    return matches[-1] if matches else ""


def _sbios_versions_activated(version, active, inactive, target="", from_version=""):
    """True only when all three FW_CPU_0 Version fields agree (and match target)."""
    if not (version and active and inactive):
        return False
    if not (version == active == inactive):
        return False
    if target:
        return _normalize_fw_version(version) == _normalize_fw_version(target)
    if from_version:
        return _normalize_fw_version(version) != _normalize_fw_version(from_version)
    return True


def _format_sbios_versions(version, active, inactive, target=""):
    bits = (
        f"Version={version or '?'} "
        f"active={active or '?'} "
        f"inactive={inactive or '?'}"
    )
    if target:
        bits += f" (want {target})"
    return bits


def _ping_host(host, timeout=2):
    """ICMP ping one host. Returns True if a reply arrived."""
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]
    try:
        import subprocess
        return subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout + 2
        ).returncode == 0
    except Exception:
        return False


def _wait_for_bmc_online_ssh_terminal(bmc_ip, job_id, timeout=900, poll_interval=10):
    """Ping + Redfish until the BMC is back; log every attempt to the SSH terminal."""
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        remaining = int(deadline - time.time())
        ping_ok = _ping_host(bmc_ip)
        redfish_ok = False
        if ping_ok:
            ok, _data, _status = _redfish_get(
                bmc_ip, "/UpdateService/FirmwareInventory/FW_CPU_0"
            )
            redfish_ok = bool(ok)
        _job_ssh_log(
            job_id,
            f"ping {bmc_ip}: {'ok' if ping_ok else 'no reply'}"
            + (f" — Redfish: {'up' if redfish_ok else 'not ready yet'}" if ping_ok else "")
            + f"  ({remaining}s left, try {attempt})",
            "info" if not redfish_ok else "out",
        )
        _job_update(
            job_id,
            phase="Waiting for BMC",
            message=(
                f"Pinging {bmc_ip} until the BMC is fully back… "
                f"({'online' if redfish_ok else 'waiting'}, {remaining}s left)"
            ),
        )
        if redfish_ok:
            _job_ssh_log(job_id, f"BMC {bmc_ip} is fully online.", "info")
            return True
        time.sleep(poll_interval)
    return False


def _sbios_run_aux_cycle(bmc_ip, job_id):
    """SSH to the BMC and run stbypowerctrl.sh aux_cycle. Returns (ok, message)."""
    _job_update(
        job_id,
        percent=99,
        phase="Standby power cycle",
        message=f"SSH root@{bmc_ip} — running stbypowerctrl.sh aux_cycle…",
    )
    _job_ssh_log(job_id, f"$ ssh root@{bmc_ip}", "cmd")
    client, ssh_err = _ssh_connect(bmc_ip)
    if ssh_err:
        _job_ssh_log(job_id, f"SSH failed: {ssh_err}", "err")
        return False, f"SSH to {bmc_ip} failed ({ssh_err})"

    _job_ssh_log(job_id, f"Connected to root@{bmc_ip}.", "info")
    try:
        cycled, detail = _bmc_aux_cycle(
            client, job_id=job_id, log_key="ssh_terminal_log"
        )
        if cycled:
            _job_ssh_log(
                job_id,
                "aux_cycle started — connection drop is expected "
                f"({detail or 'standby power cut'}).",
                "info",
            )
            return True, ""
        _job_ssh_log(job_id, f"aux_cycle failed: {detail}", "err")
        for cmd in (
            "command -v stbypowerctrl.sh || echo 'stbypowerctrl.sh not found in PATH'",
            "stbypowerctrl.sh 2>&1 | head -n 20",
        ):
            try:
                _ssh_run_logged(
                    client, cmd, job_id=job_id, timeout=30, log_key="ssh_terminal_log"
                )
            except Exception as exc:  # noqa: BLE001
                _job_ssh_log(job_id, f"({cmd} failed: {exc})", "info")
        return False, f"stbypowerctrl.sh aux_cycle failed ({detail})"
    except Exception as exc:  # noqa: BLE001
        _job_ssh_log(job_id, f"aux_cycle error: {exc}", "err")
        return False, str(exc)
    finally:
        try:
            client.close()
        except Exception:
            pass


def _sbios_confirm_all_versions(bmc_ip, job_id, target="", from_version="", attempts=36):
    """Poll FW_CPU_0 until Version/active/inactive all match. Returns (ok, version)."""
    _job_update(
        job_id,
        phase="Verifying SBIOS",
        message="Confirming all three FW_CPU_0 versions match the flashed image…",
    )
    _job_ssh_log(
        job_id,
        "Activation is complete only when Version, active slot, and inactive slot "
        "all show the target version.",
        "info",
    )
    last = ("", "", "")
    for attempt in range(attempts):
        version, active, inactive = _read_sbios_slots(bmc_ip)
        last = (version, active, inactive)
        complete = _sbios_versions_activated(
            version, active, inactive, target=target, from_version=from_version
        )
        _job_ssh_log(
            job_id,
            f"FW_CPU_0 check {attempt + 1}: "
            f"{_format_sbios_versions(version, active, inactive, target)}"
            + (" — COMPLETE" if complete else " — incomplete (need all three equal)"),
            "info" if complete else "out",
        )
        _job_update(
            job_id,
            to_version=version or None,
            message=(
                f"Confirming SBIOS… {_format_sbios_versions(version, active, inactive, target)}"
            ),
        )
        if complete:
            return True, version
        time.sleep(10)
    return False, last[0]


def _sbios_post_flash_activate(bmc_ip, job_id, from_version="", target_version=""):
    """After Redfish 100%/Completed: activate with aux_cycle until all versions match.

    Incomplete example (slots updated, top-level still old — needs aux_cycle):

        "Version": "02.07.02"   # active
        "Version": "02.07.02"   # inactive
        "Version": "02.06.06"   # top-level

    Complete only when all three equal the flashed target (e.g. 02.07.02).
    """
    target = (target_version or "").strip()
    _job_update(
        job_id,
        percent=98,
        phase="SSH activation",
        message="Checking FW_CPU_0 — all three versions must match before PASS…",
    )
    _job_ssh_log(job_id, f"=== SBIOS activation (SSH → {bmc_ip}) ===", "info")
    if target:
        _job_ssh_log(job_id, f"Target SBIOS version from package: {target}", "info")
    _job_ssh_log(
        job_id,
        "Redfish task reached 100% / Completed. Checking whether activation is done…",
        "info",
    )

    version, active, inactive = _read_sbios_slots(bmc_ip)
    _job_ssh_log(
        job_id,
        f"FW_CPU_0 after staging: {_format_sbios_versions(version, active, inactive, target)}",
        "out",
    )

    if _sbios_versions_activated(
        version, active, inactive, target=target, from_version=from_version
    ):
        _job_ssh_log(job_id, f"VERSION CONFIRMED (all three): {version}", "info")
        _job_update(job_id, to_version=version)
        return True, (
            f"SBIOS flash complete. Version is now {version}"
            + (f" (was {from_version})." if from_version else ".")
        )

    # Slots ahead of top-level Version (or anything else incomplete) → aux_cycle.
    _job_ssh_log(
        job_id,
        "Incomplete — not all three versions match the target yet. "
        "Running stbypowerctrl.sh aux_cycle to finish activation…",
        "info",
    )
    ok, err = _sbios_run_aux_cycle(bmc_ip, job_id)
    if not ok:
        return False, (
            f"The SBIOS image is staged but not fully activated: {err}. "
            f"Current FW_CPU_0: {_format_sbios_versions(version, active, inactive, target)}. "
            "SSH in and run stbypowerctrl.sh aux_cycle, then re-check until Version, "
            "active, and inactive all show the new version."
        )

    _job_ssh_log(job_id, f"Pinging {bmc_ip} until the BMC is fully back online…", "info")
    _job_update(
        job_id,
        phase="Waiting for BMC",
        message=f"Pinging {bmc_ip} until the BMC is fully on…",
    )
    time.sleep(15)
    if not _wait_for_bmc_online_ssh_terminal(bmc_ip, job_id, timeout=900):
        return False, (
            f"stbypowerctrl.sh aux_cycle was sent to {bmc_ip}, but the BMC has not "
            "come back within 15 minutes. Check the tray, then verify all three "
            "FW_CPU_0 versions."
        )

    confirmed, new_ver = _sbios_confirm_all_versions(
        bmc_ip, job_id, target=target, from_version=from_version
    )
    if confirmed:
        _job_ssh_log(job_id, f"VERSION CONFIRMED (all three): {new_ver}", "info")
        _job_update(job_id, to_version=new_ver)
        return True, (
            f"SBIOS flash complete. All three FW_CPU_0 versions are {new_ver}"
            + (f" (was {from_version})." if from_version else ".")
        )

    # Still incomplete after one cycle — try aux_cycle once more (same as manual).
    version, active, inactive = _read_sbios_slots(bmc_ip)
    _job_ssh_log(
        job_id,
        f"Still incomplete after aux_cycle: "
        f"{_format_sbios_versions(version, active, inactive, target)}. "
        "Retrying stbypowerctrl.sh aux_cycle once…",
        "info",
    )
    ok, err = _sbios_run_aux_cycle(bmc_ip, job_id)
    if not ok:
        return False, (
            f"SBIOS still incomplete after the first aux_cycle, and the retry failed: {err}. "
            f"FW_CPU_0: {_format_sbios_versions(version, active, inactive, target)}."
        )
    time.sleep(15)
    if not _wait_for_bmc_online_ssh_terminal(bmc_ip, job_id, timeout=900):
        return False, (
            "Second aux_cycle was sent but the BMC has not come back. "
            "Verify FW_CPU_0 when it returns — all three versions must match."
        )
    confirmed, new_ver = _sbios_confirm_all_versions(
        bmc_ip, job_id, target=target, from_version=from_version, attempts=24
    )
    if confirmed:
        _job_ssh_log(job_id, f"VERSION CONFIRMED (all three): {new_ver}", "info")
        _job_update(job_id, to_version=new_ver)
        return True, (
            f"SBIOS flash complete. All three FW_CPU_0 versions are {new_ver}"
            + (f" (was {from_version})." if from_version else ".")
        )

    version, active, inactive = _read_sbios_slots(bmc_ip)
    _job_ssh_log(
        job_id,
        f"VERSION UNCHANGED / incomplete: "
        f"{_format_sbios_versions(version, active, inactive, target)}",
        "err",
    )
    _job_update(job_id, to_version=version or None)
    return False, (
        "SBIOS activation incomplete — FW_CPU_0 still reports "
        f"{_format_sbios_versions(version, active, inactive, target)}. "
        "All three must equal "
        f"{target or 'the new version'}. Run stbypowerctrl.sh aux_cycle on the BMC "
        "again, then re-check."
    )


def _flash_fml_bundle(bmc_ip, image_path, job_id=None):
    """FML .fwpkg bundle: Redfish multipart with empty Targets (all components in package)."""
    return _flash_redfish_multipart(
        bmc_ip, image_path, targets=[], label="FML Bundle", job_id=job_id,
        expect_reboot=True,
    )


def _cx8_flash_target(target_id):
    for target in CX8_FLASH_TARGETS:
        if target["id"] == target_id:
            return target
    return None


def _read_cx8_versions(bmc_ip):
    return {
        "cx8_0": _read_inventory_version(bmc_ip, "FW_CX8_0"),
        "cx8_1": _read_inventory_version(bmc_ip, "FW_CX8_1"),
        "cx8_2": _read_inventory_version(bmc_ip, "FW_CX8_2"),
    }


def _flash_cx8(bmc_ip, image_path, job_id=None, verify_version=None):
    """ConnectX8 .fwpkg: Redfish multipart (package targets all CX8 devices)."""
    return _flash_redfish_multipart(
        bmc_ip, image_path, targets=[], label="CX8", job_id=job_id,
        verify_id="FW_CX8_0", verify_version=verify_version,
    )


def _local_md5(path):
    """MD5 of a local file, streamed in chunks."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _flash_bmc_no_erot(bmc_ip, image_path, job_id=None):
    """BMC w/o ERoT (.bin) — doc procedure with live terminal log in the GUI."""
    remote_final = "/run/initramfs/image-bmc"
    remote_tmp = "/run/initramfs/image-bmc.upload"
    filename = os.path.basename(image_path)

    def log(line, style="out"):
        if job_id:
            _job_log(job_id, line, style)

    def log_cmd(cmd):
        log(f"$ {cmd}", "cmd")

    if job_id:
        _job_update(job_id, percent=2, phase="Preparing", message="Computing local md5sum…")
        log("=== BMC flash (.bin) — documented SCP procedure ===", "info")
        log(f"Target BMC: {bmc_ip}", "info")
        log(f"Image: {filename}", "info")

    try:
        local_md5 = _local_md5(image_path)
    except OSError as exc:
        return False, f"Could not read local image: {exc}"

    log_cmd(f"md5sum {filename}")
    log(f"{local_md5}  {filename}", "out")

    client, err = _ssh_connect(bmc_ip)
    if err:
        log(err, "err")
        return False, err
    try:
        if job_id:
            _job_update(job_id, percent=5, phase="Copying to BMC", message="SCP to /run/initramfs/image-bmc…")

        log_cmd(f"scp {filename} root@{bmc_ip}:{remote_final}")
        last_logged_pct = -1

        def _sftp_cb(sent, total):
            nonlocal last_logged_pct
            if job_id and total:
                pct = 5 + int(40 * sent / total)
                _job_update(
                    job_id,
                    percent=pct,
                    phase="Copying to BMC",
                    message=f"SCP {sent // (1024 * 1024)}/{total // (1024 * 1024)} MB",
                )
                xfer_pct = int(100 * sent / total)
                if xfer_pct >= last_logged_pct + 10:
                    last_logged_pct = xfer_pct
                    log(
                        f"  → {sent // (1024 * 1024)}/{total // (1024 * 1024)} MB "
                        f"({xfer_pct}%)",
                        "info",
                    )

        try:
            sftp = client.open_sftp()
            try:
                sftp.put(image_path, remote_tmp, callback=_sftp_cb)
            finally:
                sftp.close()
        except OSError as exc:
            log(str(exc), "err")
            return False, f"SCP to BMC failed: {exc}"

        log(f"SCP complete → {remote_tmp}", "info")

        code, _out, mv_err = _ssh_run_logged(
            client, f"mv -f {remote_tmp} {remote_final}", job_id=job_id
        )
        if code != 0:
            return False, f"Failed to place image at {remote_final}: {mv_err or 'mv error'}"

        if job_id:
            _job_update(job_id, percent=50, phase="Verifying", message="Checking md5sum on BMC…")

        log("--- Verify md5sum on both sides (per doc) ---", "info")
        log_cmd(f"ssh root@{bmc_ip} md5sum {remote_final}")
        code, out, md5_err = _ssh_run(client, f"md5sum {remote_final}")
        if code != 0 or not out:
            log(md5_err or "no output", "err")
            return False, f"Could not read md5sum on BMC: {md5_err or 'no output'}"
        log(out, "out")
        remote_md5 = out.split()[0].strip()
        if remote_md5.lower() != local_md5.lower():
            _ssh_run(client, f"rm -f {remote_final}")
            log("md5sum MISMATCH — aborting flash", "err")
            return False, (
                "md5sum mismatch after copy — upload was corrupted, aborting flash. "
                f"local={local_md5} bmc={remote_md5}. Please retry."
            )
        log("md5sum OK — host and BMC match", "info")

        if job_id:
            _job_update(
                job_id,
                percent=55,
                phase="Flashing",
                message="Running /run/initramfs/update on BMC…",
            )

        log("--- To Update (per doc) ---", "info")
        log_cmd(f"ssh root@{bmc_ip} /run/initramfs/update")
        code, out, upd_err = _ssh_run_streaming(
            client, "/run/initramfs/update", job_id=job_id, timeout=BMC_UPDATE_TIMEOUT
        )
        combined = f"{out}\n{upd_err}".lower()

        # The updater reports this when nothing was staged at
        # /run/initramfs/image-bmc — i.e. no image to flash. Stop here with a
        # clear message instead of rebooting for nothing.
        if "no images found to update" in combined:
            log("No images found to update — nothing was flashed.", "err")
            _ssh_run(client, f"rm -f {remote_final}")
            return False, (
                "BMC reported 'No images found to update.' The image was not detected at "
                f"{remote_final}, so nothing was flashed. This usually means the file is not "
                "a valid BMC SCP flash image (use the *_scp_image.bin for this board), or the "
                "copy did not land in /run/initramfs. Nothing was changed — verify the image "
                "and retry."
            )

        channel_dropped = (
            "closed" in combined
            or "timed out" in combined
            or "connection" in combined
        )

        inplace_unsupported = (
            "mtd partition" in combined
            or "unable to find" in combined
            or "no such file" in combined
            or "not found" in combined
        )

        if code != 0 and not channel_dropped and not inplace_unsupported:
            detail = (upd_err or out or "").strip()[:400]
            return False, f"/run/initramfs/update failed: {detail or 'unknown error'}"

        used_fallback = code != 0 and inplace_unsupported

        if used_fallback:
            log(
                "In-place update not supported on this BMC — using reboot-to-flash "
                "(image stays at /run/initramfs/image-bmc)",
                "info",
            )

        _ssh_run_logged(client, f"sync; ls -l {remote_final}", job_id=job_id)

        if job_id:
            _job_update(
                job_id,
                percent=90,
                phase="Rebooting to flash",
                message=(
                    "Rebooting so initramfs flashes staged image…"
                    if used_fallback
                    else "Rebooting BMC to apply update…"
                ),
            )

        log_cmd(f"ssh root@{bmc_ip} reboot")
        _ssh_run(client, "nohup reboot >/dev/null 2>&1 &")
        log("Reboot sent — BMC will apply firmware during shutdown", "info")
        log("After finish: cold power cycle, then check BMC version", "info")

        if used_fallback:
            return True, (
                f"BMC image staged and md5 verified ({local_md5}). Rebooting to flash "
                "via initramfs. Cold power cycle afterward, then re-check version."
            )
        return True, (
            f"BMC image staged and md5 verified ({local_md5}); update ran and BMC is "
            "rebooting. Cold power cycle, then re-check version."
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------------------------------------------------------------------------
# BMC connect / status API
# ---------------------------------------------------------------------------
@app.route("/api/bmc/connect", methods=["POST"])
def connect_bmc():
    data = request.get_json(force=True, silent=True) or {}
    bmc_ip = (data.get("bmc_ip") or "").strip()
    host_ip = (data.get("host_ip") or "").strip()
    username = data.get("username", "")
    password = data.get("password", "")

    if not bmc_ip:
        return jsonify({"error": "A BMC address is required."}), 400

    cred_error = _validate_bmc_credentials(username, password)
    if cred_error:
        return jsonify({"error": cred_error}), 403

    client, err = _ssh_connect(bmc_ip)
    if err:
        return jsonify({"error": err}), 502
    try:
        _code, host, _e = _ssh_run(client, "hostname 2>/dev/null")
    finally:
        client.close()

    ver = _read_bmc_version(bmc_ip)

    return jsonify(
        {
            "ok": True,
            "message": "Connected to BMC.",
            "bmc_name": host or "BMC",
            "bmc_version": ver or None,
            "host_ip": host_ip or None,
        }
    )


OVERVIEW_FIELD_MARKER = "__MGXARC_OVERVIEW_FIELD__"


def _run_overview_batch(client):
    """Run every overview command in a single SSH exec.

    One round-trip per field made the Overview tab wait on the BMC repeatedly;
    the commands are joined with a marker and the output is split back apart.
    """
    script = f" ; printf '\\n{OVERVIEW_FIELD_MARKER}\\n' ; ".join(
        f"({command}) 2>/dev/null" for _label, command in OVERVIEW_COMMANDS
    )
    _code, out, _err = _ssh_run(client, script, timeout=OVERVIEW_BATCH_TIMEOUT)
    sections = (out or "").split(OVERVIEW_FIELD_MARKER)
    values = []
    for index in range(len(OVERVIEW_COMMANDS)):
        section = sections[index] if index < len(sections) else ""
        values.append(next(
            (line.strip() for line in section.splitlines() if line.strip()), ""
        ))
    return values


@app.route("/api/bmc/overview", methods=["GET"])
def system_overview():
    session, err = _require_bmc_session()
    if err:
        return err

    client, ssh_err = _ssh_connect(session["bmc_ip"])
    if ssh_err:
        return jsonify({"error": ssh_err}), 502

    try:
        values = _run_overview_batch(client)
    finally:
        client.close()

    summary = []
    power_state = None
    for (label, _command), value in zip(OVERVIEW_COMMANDS, values):
        summary.append({"label": label, "value": value})
        if label == "Power State" and value:
            power_state = value

    return jsonify({"summary": summary, "power_state": power_state})


def _discover_system_member(bmc_ip):
    """Return the first ComputerSystem member path under /Systems.

    Different BMCs name the member differently (e.g. 'system', 'system1',
    'Bluefield'). We read the collection instead of assuming 'system'.
    """
    ok, data, _status = _redfish_get(bmc_ip, "/Systems")
    if not ok or not isinstance(data, dict):
        return None
    for member in data.get("Members") or []:
        odata = member.get("@odata.id") if isinstance(member, dict) else None
        if odata:
            return odata.split("/redfish/v1", 1)[-1]
    return None


POWER_SYSTEM_PATH = os.environ.get("POWER_SYSTEM_PATH", "/Systems/System_0")
_POWER_SYSTEM_PATHS = {}
_POWER_SYSTEM_PATHS_LOCK = threading.Lock()


def _get_power_system(bmc_ip):
    """Read and cache the ComputerSystem resource used by the Power tab."""
    with _POWER_SYSTEM_PATHS_LOCK:
        system_path = _POWER_SYSTEM_PATHS.get(bmc_ip, POWER_SYSTEM_PATH)

    ok, data, status = _redfish_get(bmc_ip, system_path)
    if not ok and status == 404:
        discovered = _discover_system_member(bmc_ip)
        if discovered and discovered != system_path:
            system_path = discovered
            ok, data, status = _redfish_get(bmc_ip, system_path)

    if ok:
        with _POWER_SYSTEM_PATHS_LOCK:
            _POWER_SYSTEM_PATHS[bmc_ip] = system_path
    return ok, data, status, system_path


def _system_reset_info(bmc_ip):
    """Discover the ComputerSystem.Reset target + allowable ResetType values.

    Mirrors what the OpenBMC WebUI does: read the system resource and use the
    action's advertised `target` and `ResetType@Redfish.AllowableValues`.
    Falls back to the conventional path if discovery fails.
    """
    ok, data, _status, system_path = _get_power_system(bmc_ip)
    target = f"{system_path}/Actions/ComputerSystem.Reset"
    allowable = []
    if ok and isinstance(data, dict):
        reset = (data.get("Actions") or {}).get("#ComputerSystem.Reset") or {}
        tgt = reset.get("target")
        if tgt:
            target = tgt.split("/redfish/v1", 1)[-1]
        av = reset.get("ResetType@Redfish.AllowableValues")
        if isinstance(av, list):
            allowable = av
    return target, allowable, system_path


# Power control is BMC Redfish-only. These two OEM paths are intentionally
# configurable because older MGX ARC firmware may expose different member IDs.
AUX_POWER_RESET_PATH = os.environ.get(
    "AUX_POWER_RESET_PATH",
    "/Chassis/BMC_0/Actions/Oem/NvidiaChassis.AuxPowerReset",
)
BMC_MANAGER_RESET_PATH = os.environ.get(
    "BMC_MANAGER_RESET_PATH",
    "/Managers/BMC_0/Actions/Manager.Reset",
)

SYSTEM_POWER_ACTIONS = {
    "power_on": ("On", "Power-on command accepted."),
    "graceful_shutdown": ("GracefulShutdown", "Graceful shutdown command accepted."),
    "force_off": ("ForceOff", "Force-off command accepted."),
    "power_cycle": ("PowerCycle", "Host power-cycle command accepted."),
}
POWER_ACTIONS = {
    **SYSTEM_POWER_ACTIONS,
    "aux_cycle": ("AuxPowerCycle", "AUX cycle command accepted."),
    "reboot_bmc": ("GracefulRestart", "BMC reboot command accepted."),
}
# Keep old clients functional while browsers replace cached JavaScript.
POWER_ACTION_ALIASES = {
    "power_off": "force_off",
    "reboot": "reboot_bmc",
}


def _power_redfish_post(bmc_ip, path, payload, disconnect_expected=False):
    """POST a power action and account for the BMC dropping during restart."""
    url = f"{_bmc_base(bmc_ip)}{path}"
    try:
        resp = _redfish_session().post(
            url,
            auth=_bmc_auth(),
            json=payload,
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
    except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
        if disconnect_expected:
            return True, (
                "The BMC connection dropped after the command was sent; "
                "this is expected while it restarts."
            ), 202, True
        return False, "The BMC connection dropped while sending the power command.", 502, True
    except requests.exceptions.ConnectTimeout:
        return False, "Connection timed out reaching the BMC.", 504, False
    except requests.exceptions.RequestException as exc:
        return False, f"Power request failed: {exc}", 502, False

    if resp.status_code == 401:
        return False, "BMC rejected the credentials.", 401, False
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            detail = resp.text[:200]
        message = f"BMC returned HTTP {resp.status_code}."
        return False, f"{message} {detail}".strip(), resp.status_code, False
    return True, f"BMC accepted the command (HTTP {resp.status_code}).", resp.status_code, False


def _host_usb_info(bmc_ip):
    """Return optional BMC-host USB interface resources when this BMC exposes them."""
    resources = {}
    paths = {
        "host_interface": "/Managers/BMC_0/HostInterfaces/hostusb0",
        "ethernet_interface": "/Managers/BMC_0/EthernetInterfaces/hostusb0",
    }
    for key, path in paths.items():
        ok, data, _status = _redfish_get(bmc_ip, path)
        if ok and isinstance(data, dict):
            resources[key] = data
    return resources or None


@app.route("/api/bmc/power", methods=["POST"])
def system_power():
    session, err = _require_bmc_session()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    requested_action = (data.get("action") or "").strip()
    action = POWER_ACTION_ALIASES.get(requested_action, requested_action)
    if action not in POWER_ACTIONS:
        return jsonify({
            "error": (
                f"Invalid power action '{requested_action}'. "
                f"Use: {', '.join(POWER_ACTIONS)}."
            ),
        }), 400

    bmc_ip = session["bmc_ip"]
    reset_type, success_message = POWER_ACTIONS[action]
    if action in SYSTEM_POWER_ACTIONS:
        path, allowable, _system_path = _system_reset_info(bmc_ip)
        if allowable and reset_type not in allowable:
            return jsonify({
                "error": (
                    f"This BMC does not advertise ResetType '{reset_type}'. "
                    f"Allowed values: {', '.join(allowable)}."
                ),
                "action": action,
                "allowed_reset_types": allowable,
            }), 409
        payload = {"ResetType": reset_type}
        disconnect_expected = False
    elif action == "aux_cycle":
        path = AUX_POWER_RESET_PATH
        payload = {"ResetType": "AuxPowerCycle"}
        disconnect_expected = True
    else:
        path = BMC_MANAGER_RESET_PATH
        payload = {"ResetType": "GracefulRestart"}
        disconnect_expected = True

    ok, detail, status, connection_dropped = _power_redfish_post(
        bmc_ip, path, payload, disconnect_expected=disconnect_expected
    )
    endpoint = f"/redfish/v1{path}"
    if not ok:
        return jsonify({
            "error": detail,
            "endpoint": endpoint,
            "payload": payload,
            "action": action,
        }), 401 if status == 401 else 502

    return jsonify({
        "ok": True,
        "message": f"{success_message} {detail}".strip(),
        "endpoint": endpoint,
        "payload": payload,
        "http_status": status,
        "connection_dropped": connection_dropped,
        "reconnect_required": disconnect_expected,
        "action": action,
    })


@app.route("/api/bmc/power", methods=["GET"])
def system_power_state():
    """Read host power state from the logged-in BMC via Redfish."""
    session, err = _require_bmc_session()
    if err:
        return err

    bmc_ip = session["bmc_ip"]
    ok, data, status, system_path = _get_power_system(bmc_ip)
    if not ok:
        return jsonify({
            "error": data,
            "endpoint": f"/redfish/v1{system_path}",
        }), 401 if status == 401 else 502

    power_state = str(data.get("PowerState") or "Unknown")
    return jsonify({
        "ok": True,
        "power_state": power_state,
        "endpoint": f"/redfish/v1{system_path}",
        "host_usb": (
            _host_usb_info(bmc_ip)
            if request.args.get("include_host_usb", "1") != "0"
            else None
        ),
    })


@app.route("/api/bmc/os-release", methods=["GET"])
def bmc_os_release():
    """Read /etc/os-release on the BMC (verify firmware version after flash + power cycle)."""
    session, err = _require_bmc_session()
    if err:
        return err

    ok, message, output, command, version_id = _read_bmc_os_release(session["bmc_ip"])
    if not ok:
        return jsonify({
            "error": message,
            "command": command,
            "output": output,
        }), 502

    return jsonify({
        "ok": True,
        "command": command,
        "output": output,
        "version_id": version_id or None,
        "message": f"BMC reports version {version_id}." if version_id else message,
    })


def _bmc_ssh_command(bmc_ip, command):
    """Run an arbitrary shell command on the BMC over SSH."""
    command = (command or "").strip()
    if not command:
        return False, "Command is required.", "", command, 1

    client, err = _ssh_connect(bmc_ip)
    if err:
        return False, err, "", command, 1

    try:
        timeout = 300 if any(x in command for x in ("journalctl", "pldmd", "dmesg")) else SSH_CMD_TIMEOUT
        if "flashrom" in command:
            timeout = SPI_READ_TIMEOUT if " -r " in command or command.rstrip().endswith("-r") else 120
        code, out, err_out = _ssh_run(client, command, timeout=timeout)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        ).strip()
        if code != 0 and not combined:
            return False, f"Command failed (exit {code}).", "", command, code
        if code != 0:
            return True, combined or f"Command failed (exit {code}).", combined, command, code
        return True, combined or "(no output)", combined, command, code
    finally:
        try:
            client.close()
        except Exception:
            pass


def _spi_read_blank_from_hexdump(dump_output):
    """Guess whether the sampled SPI dump is blank from hexdump -C output."""
    if not dump_output:
        return None
    byte_values = []
    for line in dump_output.splitlines():
        match = re.match(r"^[0-9a-fA-F]{8}\s+(.+)$", line)
        if not match:
            continue
        hex_columns = match.group(1).split("|", 1)[0]
        byte_values.extend(
            int(value, 16)
            for value in re.findall(r"\b[0-9a-fA-F]{2}\b", hex_columns)
        )
    if not byte_values:
        return None
    erased = sum(1 for value in byte_values if value == 0xFF)
    zeros = sum(1 for value in byte_values if value == 0x00)
    if erased >= len(byte_values) * 0.85:
        return True
    if zeros >= len(byte_values) * 0.85:
        return True
    return False


def _bmc_spi_read(bmc_ip):
    """Identify and read the MGX ARC fpga1 SPI NOR through its MTD device."""
    steps = []
    client, err = _ssh_connect(bmc_ip)
    if err:
        return {"ok": False, "error": err, "steps": steps, "status_line": err}

    try:
        preflight_cmd = (
            f"test -e {SPI_READ_DEVICE} && mtdinfo {SPI_READ_DEVICE}"
        )
        code, out, err_out = _ssh_run(client, preflight_cmd, timeout=120)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        )
        steps.append({"command": preflight_cmd, "output": combined, "exit_code": code})
        if code != 0:
            return {
                "ok": False,
                "error": (
                    "SPI preflight failed — the fpga1 MTD device is unavailable "
                    "or mtdinfo could not identify it."
                ),
                "steps": steps,
                "status_line": f"SPI MTD preflight failed (exit {code})",
            }

        chip_name = None
        for line in combined.splitlines():
            if line.strip().startswith("Name:"):
                chip_name = line.split(":", 1)[-1].strip()
                break

        read_cmd = (
            f"dd if={SPI_READ_DEVICE} of={SPI_READ_IMAGE} bs=1M"
        )
        code, out, err_out = _ssh_run(client, read_cmd, timeout=SPI_READ_TIMEOUT)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        )
        steps.append({"command": read_cmd, "output": combined, "exit_code": code})
        if code != 0:
            return {
                "ok": False,
                "error": "SPI MTD read failed.",
                "steps": steps,
                "status_line": (
                    f"fpga1 identified — full SPI read failed (exit {code})"
                ),
                "chip_name": chip_name,
                "source_device": SPI_READ_DEVICE,
                "is_blank": None,
            }

        verify_cmd = (
            f"test -f {SPI_READ_IMAGE} && ls -lh {SPI_READ_IMAGE} "
            f"&& hexdump -C {SPI_READ_IMAGE} | head"
        )
        code, out, err_out = _ssh_run(client, verify_cmd, timeout=60)
        combined = "\n".join(
            line for line in f"{out or ''}\n{err_out or ''}".splitlines() if line.strip()
        )
        steps.append({"command": verify_cmd, "output": combined, "exit_code": code})

        is_blank = (
            _spi_read_blank_from_hexdump(combined) if code == 0 else None
        )
        if is_blank is True:
            status_line = "fpga1 identified — 64 MiB read complete — sampled data appears blank"
        elif is_blank is False:
            status_line = "fpga1 identified — 64 MiB read complete — firmware data present"
        else:
            status_line = (
                "fpga1 identified — full read complete — "
                "could not classify sampled contents"
            )

        return {
            "ok": True,
            "steps": steps,
            "status_line": status_line,
            "chip_name": chip_name,
            "source_device": SPI_READ_DEVICE,
            "image_path": SPI_READ_IMAGE,
            "is_blank": is_blank,
            "message": status_line,
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


def _load_i2c_config(path, default):
    """Load an optional local I2C JSON config and return data + visible error."""
    if not os.path.isfile(path):
        return default, f"Config file not found: {os.path.basename(path)}"
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            raise ValueError("top-level value must be an object")
        return data, None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return default, f"{os.path.basename(path)}: {exc}"


def _i2c_configs():
    bus_map, bus_error = _load_i2c_config(
        I2C_BUS_MAP_FILE, {"buses": {}, "mux_paths": []}
    )
    expected, expected_error = _load_i2c_config(
        I2C_EXPECTED_DEVICES_FILE, {"buses": {}}
    )
    return bus_map, expected, [
        error for error in (bus_error, expected_error) if error
    ]


def _parse_i2c_number(value, label, minimum, maximum):
    """Parse a decimal or 0x-prefixed I2C field without allowing shell syntax."""
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number.")
    text = str(value).strip().lower()
    if re.fullmatch(r"[0-9]+", text):
        number = int(text, 10)
    elif re.fullmatch(r"0x[0-9a-f]+", text):
        number = int(text, 16)
    else:
        raise ValueError(
            f"{label} must be decimal or hexadecimal (for example 32 or 0x20)."
        )
    if number < minimum or number > maximum:
        raise ValueError(
            f"{label} must be between {minimum:#x} and {maximum:#x}."
        )
    return number


def _i2c_bus_metadata(bus, bus_map):
    configured = (bus_map.get("buses") or {}).get(str(bus)) or {}
    return {
        "number": bus,
        "name": configured.get("name") or f"I2C Bus {bus}",
        "subsystem": configured.get("subsystem") or "",
        "description": configured.get("description") or "",
    }


def _parse_i2c_detect(output):
    """Return responding 7-bit addresses from the standard i2cdetect grid."""
    found = []
    for line in (output or "").splitlines():
        match = re.match(r"^\s*([0-9a-fA-F]{2}):(.*)$", line)
        if not match:
            continue
        row = int(match.group(1), 16)
        cells = match.group(2)
        for token_match in re.finditer(r"UU|[0-9a-fA-F]{2}|--", cells):
            column = max(0, (token_match.start() - 1) // 3)
            address = row + column
            token = token_match.group(0)
            if token == "UU" or re.fullmatch(r"[0-9a-fA-F]{2}", token):
                if 0x03 <= address <= 0x77:
                    found.append(address)
    return sorted(set(found))


def _expected_i2c_devices(bus, expected_config):
    devices = (expected_config.get("buses") or {}).get(str(bus)) or []
    parsed = []
    for item in devices:
        if not isinstance(item, dict):
            continue
        try:
            address = _parse_i2c_number(
                item.get("address"), "Expected device address", 0x03, 0x77
            )
        except ValueError:
            continue
        parsed.append({
            "address": address,
            "address_hex": f"0x{address:02x}",
            "name": item.get("name") or f"Device 0x{address:02x}",
            "description": item.get("description") or "",
        })
    return parsed


def _run_i2c_ssh(bmc_ip, command):
    client, err = _ssh_connect(bmc_ip)
    if err:
        return None, "", err
    try:
        code, out, err_out = _ssh_run(
            client, command, timeout=I2C_CMD_TIMEOUT
        )
    finally:
        client.close()
    output = "\n".join(
        part for part in ((out or "").strip(), (err_out or "").strip()) if part
    ).strip()
    return code, output, None


def _i2c_command(bmc_ip, action, payload=None):
    """Run one validated, server-built i2c-tools operation on the BMC."""
    payload = payload or {}
    bus_map, expected_config, config_errors = _i2c_configs()
    bus = address = register = value = None
    mode = str(payload.get("mode") or "b").strip().lower()
    try:
        if action != "buses":
            bus = _parse_i2c_number(payload.get("bus"), "Bus", 0, 4095)
        if action in ("dump", "read", "write"):
            address = _parse_i2c_number(
                payload.get("address"), "Device address", 0x03, 0x77
            )
        if action in ("read", "write"):
            register = _parse_i2c_number(
                payload.get("register"), "Register", 0x00, 0xFF
            )
        if action == "write":
            value = _parse_i2c_number(payload.get("value"), "Value", 0x00, 0xFFFF)
            if payload.get("confirm") is not True:
                raise ValueError("I2C writes require explicit confirmation.")
        allowed_modes = {
            "dump": {"b", "w"},
            "read": {"b", "w"},
            "write": {"b", "w"},
        }
        if action in allowed_modes and mode not in allowed_modes[action]:
            raise ValueError(
                f"Mode '{mode}' is invalid for {action}. "
                f"Use: {', '.join(sorted(allowed_modes[action]))}."
            )
        if action == "write" and mode == "b" and value > 0xFF:
            raise ValueError("Byte-mode write value must be between 0x00 and 0xff.")
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}

    if action == "buses":
        command = "i2cdetect -l"
    elif action == "detect":
        command = f"i2cdetect -y {bus}"
    elif action == "dump":
        command = f"i2cdump -y {bus} 0x{address:02x} {mode}"
    elif action == "read":
        command = f"i2cget -y {bus} 0x{address:02x} 0x{register:02x} {mode}"
    elif action == "write":
        command = (
            f"i2cset -y {bus} 0x{address:02x} "
            f"0x{register:02x} 0x{value:x} {mode}"
        )
    else:
        return {"ok": False, "error": "Unknown I2C operation.", "status": 400}
    if action != "buses":
        command = (
            f"test -e /dev/i2c-{bus} || "
            f"{{ echo 'I2C bus {bus} not found (/dev/i2c-{bus}).' >&2; exit 2; }}; "
            f"{command}"
        )

    code, output, connection_error = _run_i2c_ssh(bmc_ip, command)
    bus_meta = _i2c_bus_metadata(bus, bus_map) if bus is not None else None
    result = {
        "ok": code == 0 if code is not None else False,
        "action": action,
        "bmc_ip": bmc_ip,
        "bus": bus_meta,
        "command": command,
        "output": output or "(no output)",
        "exit_code": code,
        "config_errors": config_errors,
    }
    if connection_error:
        result.update({"error": connection_error, "status": 502})
        return result
    if code != 0:
        lower_output = output.lower()
        if "bus" in lower_output and "not found" in lower_output:
            error = f"I2C bus {bus} was not found on the BMC."
        elif action in ("dump", "read", "write") and any(
            marker in lower_output
            for marker in (
                "read failed", "write failed", "no such device",
                "remote i/o error", "device or resource busy",
            )
        ):
            error = (
                f"I2C device 0x{address:02x} did not respond on bus {bus}. "
                "Check the bus, mux path, and device address."
            )
        else:
            error = output or f"I2C command failed (exit {code})."
        result.update({
            "error": error,
            "status": 502,
        })
        return result

    if action == "buses":
        buses = []
        for line in output.splitlines():
            match = re.match(r"^i2c-(\d+)\s+(\S+)\s+(.+)$", line.strip())
            if not match:
                continue
            number = int(match.group(1))
            item = _i2c_bus_metadata(number, bus_map)
            item.update({
                "type": match.group(2),
                "adapter": match.group(3).strip(),
            })
            buses.append(item)
        result["buses"] = buses
        result["mux_paths"] = [
            {
                "id": item.get("id"),
                "name": item.get("name") or item.get("id"),
                "description": item.get("description") or "",
            }
            for item in (bus_map.get("mux_paths") or [])
            if isinstance(item, dict) and item.get("id") and item.get("command")
        ]
        result["parsed_result"] = f"Found {len(buses)} I2C bus(es)."
    elif action == "detect":
        found = _parse_i2c_detect(output)
        expected = _expected_i2c_devices(bus, expected_config)
        found_set = set(found)
        checks = [
            {**device, "present": device["address"] in found_set}
            for device in expected
        ]
        missing = [device for device in checks if not device["present"]]
        validation = (
            "not_configured" if not checks else "pass" if not missing else "fail"
        )
        result.update({
            "found_addresses": [f"0x{item:02x}" for item in found],
            "expected_devices": checks,
            "validation": validation,
            "parsed_result": (
                f"{len(found)} responding address(es); "
                + (
                    "no expected-device mapping configured."
                    if validation == "not_configured"
                    else "all expected devices responded."
                    if validation == "pass"
                    else f"{len(missing)} expected device(s) missing."
                )
            ),
        })
    elif action == "read":
        match = re.search(r"0x[0-9a-fA-F]+", output)
        result["value"] = match.group(0).lower() if match else None
        result["parsed_result"] = (
            f"Register 0x{register:02x} returned {result['value']}."
            if result["value"] else "Read completed; no hex value was parsed."
        )
    elif action == "dump":
        result["parsed_result"] = (
            f"Register dump completed for device 0x{address:02x}."
        )
    elif action == "write":
        result["parsed_result"] = (
            f"Wrote 0x{value:x} to register 0x{register:02x}."
        )
        if payload.get("readback") is True:
            read_command = (
                f"i2cget -y {bus} 0x{address:02x} 0x{register:02x} {mode}"
            )
            read_code, read_output, read_error = _run_i2c_ssh(
                bmc_ip, read_command
            )
            result["readback"] = {
                "ok": read_code == 0,
                "command": read_command,
                "output": read_output or read_error or "(no output)",
                "exit_code": read_code,
            }
            if read_code != 0:
                result["ok"] = False
                result["error"] = "Write completed, but readback failed."
                result["status"] = 502
    return result


def _i2c_set_mux(bmc_ip, path_id):
    """Run a fixed, config-defined mux command; never accept commands from UI."""
    bus_map, _expected, config_errors = _i2c_configs()
    paths = bus_map.get("mux_paths") or []
    selected = next(
        (
            item for item in paths
            if isinstance(item, dict) and item.get("id") == path_id
        ),
        None,
    )
    if not selected:
        return {
            "ok": False,
            "error": f"Unknown mux path '{path_id}'.",
            "status": 400,
            "config_errors": config_errors,
        }
    command = str(selected.get("command") or "").strip()
    if not command:
        return {
            "ok": False,
            "error": f"Mux path '{path_id}' has no configured backend command.",
            "status": 400,
            "config_errors": config_errors,
        }
    code, output, connection_error = _run_i2c_ssh(bmc_ip, command)
    result = {
        "ok": code == 0 if code is not None else False,
        "action": "mux",
        "bmc_ip": bmc_ip,
        "mux_path": {
            "id": selected.get("id"),
            "name": selected.get("name") or selected.get("id"),
            "description": selected.get("description") or "",
        },
        "command": command,
        "output": output or "(no output)",
        "exit_code": code,
        "config_errors": config_errors,
    }
    if connection_error or code != 0:
        result["error"] = connection_error or output or "Mux command failed."
        result["status"] = 502
    else:
        result["parsed_result"] = (
            f"Mux path '{result['mux_path']['name']}' selected."
        )
    return result


@app.route("/api/bmc/spi-read", methods=["POST"])
def bmc_spi_read():
    """Read the fpga1 SPI NOR through its MTD device on the connected BMC."""
    session, err = _require_bmc_session()
    if err:
        return err

    result = _bmc_spi_read(session["bmc_ip"])
    if not result.get("ok"):
        return jsonify(result), 502
    return jsonify(result)


@app.route("/api/bmc/i2c/buses", methods=["GET"])
def bmc_i2c_buses():
    """List I2C adapters exposed by the connected BMC."""
    session, err = _require_bmc_session()
    if err:
        return err
    result = _i2c_command(session["bmc_ip"], "buses")
    status = result.pop("status", 200)
    return jsonify(result), status


@app.route("/api/bmc/i2c/mux", methods=["POST"])
def bmc_i2c_mux():
    """Select a config-defined I2C mux path on the connected BMC."""
    session, err = _require_bmc_session()
    if err:
        return err
    payload = request.get_json(force=True, silent=True) or {}
    path_id = str(payload.get("path_id") or "").strip()
    if not path_id:
        return jsonify({"error": "Mux path is required."}), 400
    result = _i2c_set_mux(session["bmc_ip"], path_id)
    status = result.pop("status", 200)
    return jsonify(result), status


@app.route("/api/bmc/i2c/<action>", methods=["POST"])
def bmc_i2c_operation(action):
    """Detect, dump, read, or write through i2c-tools on the BMC."""
    session, err = _require_bmc_session()
    if err:
        return err
    if action not in ("detect", "dump", "read", "write"):
        return jsonify({"error": "Unknown I2C operation."}), 404

    payload = request.get_json(force=True, silent=True) or {}
    result = _i2c_command(session["bmc_ip"], action, payload)
    status = result.pop("status", 200)
    return jsonify(result), status


@app.route("/api/bmc/command", methods=["POST"])
def bmc_run_command():
    """Run a manual shell command on the connected BMC over SSH."""
    session, err = _require_bmc_session()
    if err:
        return err

    data = request.get_json(force=True, silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "Command is required."}), 400

    ok, message, output, cmd, exit_code = _bmc_ssh_command(session["bmc_ip"], command)
    if not ok:
        return jsonify({
            "error": message,
            "command": cmd,
            "output": output,
            "exit_code": exit_code,
        }), 502

    return jsonify({
        "ok": True,
        "command": cmd,
        "output": output,
        "message": message,
        "exit_code": exit_code,
    })


@app.route("/api/bmc/sensors", methods=["GET"])
def system_sensors():
    session, err = _require_bmc_session()
    if err:
        return err

    sensors = {"temperatures": [], "voltages": [], "fans": [], "power": []}

    ok, thermal, _ = _redfish_get(session["bmc_ip"], "/Chassis/chassis/Thermal")
    if ok:
        for t in thermal.get("Temperatures", []):
            sensors["temperatures"].append(
                {
                    "name": t.get("Name"),
                    "reading": t.get("ReadingCelsius"),
                    "upper_critical": t.get("UpperThresholdCritical"),
                    "health": (t.get("Status") or {}).get("Health"),
                }
            )
        for f in thermal.get("Fans", []):
            sensors["fans"].append(
                {
                    "name": f.get("Name"),
                    "reading": f.get("Reading"),
                    "units": f.get("ReadingUnits", "RPM"),
                    "health": (f.get("Status") or {}).get("Health"),
                }
            )

    ok, power, _ = _redfish_get(session["bmc_ip"], "/Chassis/chassis/Power")
    if ok:
        for v in power.get("Voltages", []):
            sensors["voltages"].append(
                {
                    "name": v.get("Name"),
                    "reading": v.get("ReadingVolts"),
                    "health": (v.get("Status") or {}).get("Health"),
                }
            )
        for pc in power.get("PowerControl", []):
            sensors["power"].append(
                {
                    "name": pc.get("Name"),
                    "consumed_watts": pc.get("PowerConsumedWatts"),
                    "capacity_watts": pc.get("PowerCapacityWatts"),
                }
            )
    return jsonify(sensors)


@app.route("/api/bmc/firmware", methods=["GET"])
def system_firmware():
    session, err = _require_bmc_session()
    if err:
        return err

    inventory, inv_err = _fetch_firmware_inventory(session["bmc_ip"])
    if inv_err and not inventory:
        return jsonify({"error": inv_err}), 502

    parts = []
    matched_ids = set()
    for part in MGX_ARC_FIRMWARE_PARTS:
        member_id, entry = _match_inventory_member(part, inventory)
        entry = entry or {}
        if member_id:
            matched_ids.add(member_id)
        version = entry.get("version", "")
        source = "redfish" if version else "not found"

        if not version and part.get("host_cmd") and session.get("host_ip"):
            version = _read_host_version(session["host_ip"], part["host_cmd"])
            if version:
                source = "host"

        parts.append(
            {
                "name": part["name"],
                "redfish_id": member_id or part.get("redfish_id", ""),
                "version": version,
                "health": entry.get("health"),
                "source": source,
                "notes": part.get("notes"),
            }
        )

    # Surface any inventory members the BMC reports that aren't mapped to a
    # configured part, so nothing is hidden (e.g. extra CX8/GPU/SMA devices).
    for member_id, entry in inventory.items():
        if member_id in matched_ids:
            continue
        parts.append(
            {
                "name": entry.get("name") or member_id,
                "redfish_id": member_id,
                "version": entry.get("version", ""),
                "health": entry.get("health"),
                "source": "redfish" if entry.get("version") else "not found",
                "notes": "Additional device reported by BMC inventory.",
                "extra": True,
            }
        )

    return jsonify({"parts": parts, "inventory_count": len(inventory)})


@app.route("/api/bmc/eventlog", methods=["GET"])
def system_eventlog():
    session, err = _require_bmc_session()
    if err:
        return err

    ok, log, status = _redfish_get(
        session["bmc_ip"],
        "/Systems/system/LogServices/EventLog/Entries",
    )
    if not ok:
        return jsonify({"error": log}), status

    entries = []
    for e in log.get("Members", []):
        entries.append(
            {
                "id": e.get("Id"),
                "severity": e.get("Severity"),
                "created": e.get("Created"),
                "message": e.get("Message"),
            }
        )
    entries.sort(key=lambda x: x.get("created") or "", reverse=True)
    return jsonify({"entries": entries[:200]})


@app.route("/api/bmc/flash/jobs", methods=["GET"])
def flash_jobs_list():
    """List flash jobs for this system (newest first)."""
    session, err = _require_bmc_session()
    if err:
        return err
    jobs = sorted(
        _jobs_for_bmc(session["bmc_ip"]),
        key=lambda j: j.get("created_at", 0),
        reverse=True,
    )
    return jsonify({"jobs": jobs})


@app.route("/api/bmc/flash/jobs/<job_id>", methods=["GET"])
def flash_job_status(job_id):
    """Poll progress for one flash job."""
    session, err = _require_bmc_session()
    if err:
        return err
    job = _job_snapshot(job_id)
    if not job or job.get("bmc_ip") != session["bmc_ip"]:
        return jsonify({"error": "Flash job not found."}), 404
    return jsonify(job)


@app.route("/api/bmc/flash/stage", methods=["POST"])
def flash_stage():
    """Accept a .zip, folder, or file(s); unzip/scan and return flashable images."""
    session, err = _require_bmc_session()
    if err:
        return err

    bmc_ip = session["bmc_ip"]
    bundle_id = uuid.uuid4().hex[:16]
    staging_dir = tempfile.mkdtemp(prefix=f"mgx-flash-{bundle_id}-")

    archive = request.files.get("archive")
    uploads = request.files.getlist("files")

    try:
        if archive and archive.filename:
            ext = os.path.splitext(archive.filename)[1].lower()
            if ext == ".zip":
                zip_tmp = os.path.join(staging_dir, "upload.zip")
                archive.save(zip_tmp)
                with zipfile.ZipFile(zip_tmp, "r") as zf:
                    zf.extractall(staging_dir)
                os.unlink(zip_tmp)
            else:
                rel = _safe_upload_relpath(os.path.basename(archive.filename))
                dest = os.path.join(staging_dir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                archive.save(dest)
        elif uploads:
            for uf in uploads:
                if not uf.filename:
                    continue
                rel = _safe_upload_relpath(uf.filename.replace("\\", "/"))
                dest = os.path.join(staging_dir, rel.replace("/", os.sep))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                uf.save(dest)
        else:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return jsonify({"error": "Upload a .zip archive, folder, or firmware file(s)."}), 400

        _extract_nested_zips(staging_dir)
        candidates = _scan_flash_candidates(staging_dir)
        if not candidates:
            shutil.rmtree(staging_dir, ignore_errors=True)
            return jsonify({
                "error": (
                    "No firmware files found (.fwpkg, .image, .img, .bin, .tar). "
                    "Pick a folder or zip that contains them."
                ),
            }), 400

        with _FLASH_BUNDLE_LOCK:
            _FLASH_BUNDLES[bundle_id] = {
                "dir": staging_dir,
                "files": candidates,
                "created": time.time(),
                "bmc_ip": bmc_ip,
            }

        return jsonify({
            "bundle_id": bundle_id,
            "files": candidates,
            "message": f"Found {len(candidates)} firmware file(s). Choose one to flash.",
        })
    except zipfile.BadZipFile:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return jsonify({"error": "The uploaded file is not a valid .zip archive."}), 400
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return jsonify({"error": f"Could not process upload: {exc}"}), 500


@app.route("/api/bmc/flash/bmc", methods=["GET"])
def bmc_flash_options():
    """Return current BMC version and available flash targets."""
    session, err = _require_bmc_session()
    if err:
        return err

    current = _read_bmc_version(session["bmc_ip"])
    targets = []
    for target in BMC_FLASH_TARGETS:
        bundled = _bundled_image_path(target["image_file"])
        targets.append(
            {
                **target,
                "bundled_available": os.path.isfile(bundled),
                "bundled_path": bundled if os.path.isfile(bundled) else None,
            }
        )
    return jsonify({"current_version": current, "targets": targets})


@app.route("/api/bmc/flash/bmc", methods=["POST"])
def bmc_flash_start():
    """Flash the BMC from an uploaded firmware file.

    The flash method is auto-detected from the file extension:
      .fwpkg / .image / .img / .tar  → Redfish update-multipart (GUI flow)
      .bin                           → SCP to /run/initramfs/image-bmc + update
    No preset version is assumed, so any firmware file works as they change.
    """
    session, err = _require_bmc_session()
    if err:
        return err

    bmc_ip = session["bmc_ip"]
    image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
    if img_err:
        return img_err
    if not image_path or not os.path.isfile(image_path):
        return jsonify({"error": "Image file not found on server."}), 404

    ext = os.path.splitext(orig_name)[1].lower()
    current = _read_bmc_version(bmc_ip)

    try:
        if ext == ".bin":
            method = "initramfs_scp"
            curl_cmd = _initramfs_curl_hint(bmc_ip, image_path)
            worker = lambda job_id, fv=current: _flash_bmc_gui(
                bmc_ip, image_path, job_id, initramfs=True, from_version=fv,
            )
        else:
            method = "redfish_multipart"
            curl_cmd = _redfish_curl_hint(bmc_ip, image_path, [])
            worker = lambda job_id, fv=current: _flash_bmc_gui(
                bmc_ip, image_path, job_id, initramfs=False, from_version=fv,
            )

        job_id = _enqueue_flash(
            bmc_ip,
            f"BMC — {orig_name}",
            method,
            curl_cmd,
            worker,
            cleanup_path=cleanup_path,
            extra={
                "from_version": current,
                "image_file": orig_name,
            },
        )
    except Exception:
        if cleanup_path and os.path.exists(cleanup_path):
            os.unlink(cleanup_path)
        raise

    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "from_version": current,
            "image_file": orig_name,
        }
    ), 202


@app.route("/api/bmc/flash/smr", methods=["GET"])
def smr_flash_options():
    """Return current SMR version and bundled image availability."""
    session, err = _require_bmc_session()
    if err:
        return err

    bundled = _bundled_image_path(SMR_FLASH_TARGET["image_file"])
    return jsonify(
        {
            "current_version": _read_smr_version(session["bmc_ip"]),
            "target": {
                **SMR_FLASH_TARGET,
                "bundled_available": os.path.isfile(bundled),
            },
        }
    )


@app.route("/api/bmc/flash/smr", methods=["POST"])
def smr_flash_start():
    """Flash FPGA / SMR to the bundled or uploaded signed image."""
    session, err = _require_bmc_session()
    if err:
        return err

    image_path = None
    cleanup_path = None
    force = False
    orig_name = ""

    if request.content_type and "multipart/form-data" in request.content_type:
        force = (request.form.get("force") or "").lower() in ("1", "true", "yes")
        bmc_ip = session["bmc_ip"]
        image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
        if img_err:
            return img_err
    else:
        data = request.get_json(force=True, silent=True) or {}
        force = bool(data.get("force"))
        if data.get("use_bundled"):
            image_path = _bundled_image_path(SMR_FLASH_TARGET["image_file"])
            if not os.path.isfile(image_path):
                return jsonify(
                    {
                        "error": (
                            f"Bundled SMR image not found: {SMR_FLASH_TARGET['image_file']}. "
                            "Place it in firmware_images/ or upload a file."
                        )
                    }
                ), 404

    if not image_path:
        return jsonify({"error": "An SMR image file is required (bundled or upload)."}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "SMR image file not found on server."}), 404

    current = _read_smr_version(session["bmc_ip"])
    target_ver = SMR_FLASH_TARGET["version"]
    if not force and current and _normalize_fw_version(current) == _normalize_fw_version(target_ver):
        if cleanup_path:
            os.unlink(cleanup_path)
        return jsonify(
            {
                "error": (
                    f"SMR is already on {target_ver}. "
                    "Check Force reflash to flash again."
                )
            }
        ), 409

    bmc_ip = session["bmc_ip"]
    curl_cmd = _redfish_curl_hint(
        bmc_ip, image_path, [SMR_FLASH_TARGET["redfish_target"]]
    )
    job_id = _enqueue_flash(
        bmc_ip,
        f"SMR — {target_ver}",
        "redfish_multipart",
        curl_cmd,
        lambda job_id: _flash_smr(bmc_ip, image_path, job_id=job_id),
        cleanup_path=cleanup_path,
        extra={"from_version": current, "to_version": target_ver},
    )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "from_version": current,
            "to_version": target_ver,
            "target": SMR_FLASH_TARGET,
        }
    ), 202


@app.route("/api/bmc/flash/erot", methods=["GET"])
def erot_flash_options():
    """Return current ERoT version and bundled image availability."""
    session, err = _require_bmc_session()
    if err:
        return err

    bundled = _bundled_image_path(EROT_FLASH_TARGET["image_file"])
    return jsonify(
        {
            "current_version": _read_erot_version(session["bmc_ip"]),
            "target": {
                **EROT_FLASH_TARGET,
                "bundled_available": os.path.isfile(bundled),
            },
        }
    )


@app.route("/api/bmc/flash/erot", methods=["POST"])
def erot_flash_start():
    """Flash ERoT to the bundled or uploaded ecfw image."""
    session, err = _require_bmc_session()
    if err:
        return err

    image_path = None
    cleanup_path = None
    force = False
    orig_name = ""

    if request.content_type and "multipart/form-data" in request.content_type:
        force = (request.form.get("force") or "").lower() in ("1", "true", "yes")
        bmc_ip = session["bmc_ip"]
        image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
        if img_err:
            return img_err
    else:
        data = request.get_json(force=True, silent=True) or {}
        force = bool(data.get("force"))
        if data.get("use_bundled"):
            image_path = _bundled_image_path(EROT_FLASH_TARGET["image_file"])
            if not os.path.isfile(image_path):
                return jsonify(
                    {
                        "error": (
                            f"Bundled ERoT image not found: {EROT_FLASH_TARGET['image_file']}. "
                            "Place it in firmware_images/ or upload a file."
                        )
                    }
                ), 404

    if not image_path:
        return jsonify({"error": "An ERoT image file is required (bundled or upload)."}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "ERoT image file not found on server."}), 404

    current = _read_erot_version(session["bmc_ip"])
    target_ver = EROT_FLASH_TARGET["version"]
    if not force and current and _normalize_fw_version(current) == _normalize_fw_version(target_ver):
        if cleanup_path:
            os.unlink(cleanup_path)
        return jsonify(
            {
                "error": (
                    f"ERoT is already on {target_ver}. "
                    "Check Force reflash to flash again."
                )
            }
        ), 409

    bmc_ip = session["bmc_ip"]
    curl_cmd = _redfish_curl_hint(
        bmc_ip, image_path, [EROT_FLASH_TARGET["redfish_target"]]
    )
    job_id = _enqueue_flash(
        bmc_ip,
        f"ERoT — {target_ver}",
        "redfish_multipart",
        curl_cmd,
        lambda job_id: _flash_erot(bmc_ip, image_path, job_id=job_id),
        cleanup_path=cleanup_path,
        extra={"from_version": current, "to_version": target_ver},
    )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "from_version": current,
            "to_version": target_ver,
            "target": EROT_FLASH_TARGET,
        }
    ), 202


@app.route("/api/bmc/flash/sbios", methods=["GET"])
def sbios_flash_options():
    """Return current SBIOS/UEFI version and bundled image availability."""
    session, err = _require_bmc_session()
    if err:
        return err

    bundled = _bundled_image_path(SBIOS_FLASH_TARGET["image_file"])
    return jsonify(
        {
            "current_version": _read_sbios_version(session["bmc_ip"]),
            "target": {
                **SBIOS_FLASH_TARGET,
                "bundled_available": os.path.isfile(bundled),
            },
        }
    )


@app.route("/api/bmc/flash/sbios", methods=["POST"])
def sbios_flash_start():
    """Flash SBIOS/UEFI from an uploaded .fwpkg (same Redfish flow as BMC flash)."""
    session, err = _require_bmc_session()
    if err:
        return err

    bmc_ip = session["bmc_ip"]
    image_path = None
    cleanup_path = None
    orig_name = None
    force = False

    if request.content_type and "multipart/form-data" in request.content_type:
        force = (request.form.get("force") or "").lower() in ("1", "true", "yes")
        image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
        if img_err:
            return img_err
    else:
        data = request.get_json(force=True, silent=True) or {}
        force = bool(data.get("force"))
        if data.get("use_bundled"):
            image_path = _bundled_image_path(SBIOS_FLASH_TARGET["image_file"])
            orig_name = SBIOS_FLASH_TARGET["image_file"]
            if not os.path.isfile(image_path):
                return jsonify(
                    {
                        "error": (
                            f"Bundled SBIOS image not found: {SBIOS_FLASH_TARGET['image_file']}. "
                            "Place it in firmware_images/ or upload a file."
                        )
                    }
                ), 404

    if not image_path:
        return jsonify({"error": "An SBIOS image file is required (bundled or upload)."}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "SBIOS image file not found on server."}), 404

    orig_name = orig_name or os.path.basename(image_path)
    current = _read_sbios_version(bmc_ip)
    target_ver = _sbios_target_from_filename(orig_name)

    try:
        curl_cmd = _redfish_curl_hint(
            bmc_ip, image_path, [SBIOS_FLASH_TARGET["redfish_target"]]
        )
        job_id = _enqueue_flash(
            bmc_ip,
            f"SBIOS — {orig_name}",
            "redfish_multipart",
            curl_cmd,
            lambda job_id, fv=current, tv=target_ver: _flash_sbios_gui(
                bmc_ip, image_path, job_id, from_version=fv, target_version=tv,
            ),
            cleanup_path=cleanup_path,
            extra={
                "from_version": current,
                "to_version": target_ver or None,
                "image_file": orig_name,
            },
        )
    except Exception:
        if cleanup_path and os.path.exists(cleanup_path):
            os.unlink(cleanup_path)
        raise

    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "from_version": current,
            "to_version": target_ver or None,
            "image_file": orig_name,
        }
    ), 202


@app.route("/api/bmc/flash/fml", methods=["GET"])
def fml_flash_options():
    """Return FML bundle metadata and bundled .fwpkg availability."""
    session, err = _require_bmc_session()
    if err:
        return err

    bundled = _bundled_image_path(FML_BUNDLE_FLASH["image_file"])
    return jsonify(
        {
            "bundle": {
                **FML_BUNDLE_FLASH,
                "bundled_available": os.path.isfile(bundled),
            }
        }
    )


@app.route("/api/bmc/flash/fml", methods=["POST"])
def fml_flash_start():
    """Flash the full FML nvfw bundle (.fwpkg)."""
    session, err = _require_bmc_session()
    if err:
        return err

    image_path = None
    cleanup_path = None

    if request.content_type and "multipart/form-data" in request.content_type:
        bmc_ip = session["bmc_ip"]
        image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
        if img_err:
            return img_err
    else:
        data = request.get_json(force=True, silent=True) or {}
        if data.get("use_bundled"):
            image_path = _bundled_image_path(FML_BUNDLE_FLASH["image_file"])
            if not os.path.isfile(image_path):
                return jsonify(
                    {
                        "error": (
                            f"Bundled FML package not found: {FML_BUNDLE_FLASH['image_file']}. "
                            "Place it in firmware_images/ or upload a file."
                        )
                    }
                ), 404

    if not image_path:
        return jsonify({"error": "An FML bundle (.fwpkg) is required (bundled or upload)."}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "FML bundle file not found on server."}), 404

    bmc_ip = session["bmc_ip"]
    curl_cmd = _redfish_curl_hint(bmc_ip, image_path, [])
    job_id = _enqueue_flash(
        bmc_ip,
        "FML Bundle",
        "redfish_multipart",
        curl_cmd,
        lambda job_id: _flash_fml_bundle(bmc_ip, image_path, job_id=job_id),
        cleanup_path=cleanup_path,
        extra={"bundle": FML_BUNDLE_FLASH["bundle_id"]},
    )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "bundle": FML_BUNDLE_FLASH,
        }
    ), 202


@app.route("/api/bmc/flash/cx8", methods=["GET"])
def cx8_flash_options():
    """Return current CX8 versions and PK/QP flash package options."""
    session, err = _require_bmc_session()
    if err:
        return err

    targets = []
    for target in CX8_FLASH_TARGETS:
        bundled = _bundled_image_path(target["image_file"])
        targets.append({**target, "bundled_available": os.path.isfile(bundled)})

    return jsonify(
        {
            "current_versions": _read_cx8_versions(session["bmc_ip"]),
            "redfish_ids": CX8_REDFISH_IDS,
            "targets": targets,
        }
    )


@app.route("/api/bmc/flash/cx8", methods=["POST"])
def cx8_flash_start():
    """Flash ConnectX8 using PK or QP .fwpkg package."""
    session, err = _require_bmc_session()
    if err:
        return err

    target_id = None
    image_path = None
    cleanup_path = None
    force = False

    if request.content_type and "multipart/form-data" in request.content_type:
        target_id = (request.form.get("target_id") or "").strip()
        force = (request.form.get("force") or "").lower() in ("1", "true", "yes")
        bmc_ip = session["bmc_ip"]
        image_path, cleanup_path, orig_name, img_err = _image_from_flash_request(bmc_ip)
        if img_err:
            return img_err
    else:
        data = request.get_json(force=True, silent=True) or {}
        target_id = (data.get("target_id") or "").strip()
        force = bool(data.get("force"))
        if data.get("use_bundled"):
            target = _cx8_flash_target(target_id)
            if not target:
                return jsonify({"error": "Unknown CX8 variant (pk or qp)."}), 400
            image_path = _bundled_image_path(target["image_file"])
            if not os.path.isfile(image_path):
                return jsonify(
                    {
                        "error": (
                            f"Bundled CX8 package not found: {target['image_file']}. "
                            f"Copy {target['source_file']} to firmware_images/ "
                            f"as {target['image_file']} or upload a file."
                        )
                    }
                ), 404

    if not target_id:
        return jsonify({"error": "target_id is required (pk or qp)."}), 400
    if not image_path:
        return jsonify({"error": "A CX8 .fwpkg is required (bundled or upload)."}), 400
    if not os.path.isfile(image_path):
        return jsonify({"error": "CX8 package file not found on server."}), 404

    target = _cx8_flash_target(target_id)
    if not target:
        if cleanup_path:
            os.unlink(cleanup_path)
        return jsonify({"error": "Unknown CX8 variant. Use pk or qp."}), 400

    current = _read_cx8_versions(session["bmc_ip"])
    target_ver = target["version"]
    if not force and all(
        _normalize_fw_version(v) == _normalize_fw_version(target_ver)
        for v in current.values() if v
    ) and any(current.values()):
        if cleanup_path:
            os.unlink(cleanup_path)
        return jsonify(
            {
                "error": (
                    f"All CX8 devices already report {target_ver}. "
                    "Check Force reflash to flash again."
                )
            }
        ), 409

    bmc_ip = session["bmc_ip"]
    curl_cmd = _redfish_curl_hint(bmc_ip, image_path, [])
    job_id = _enqueue_flash(
        bmc_ip,
        f"CX8 {target['variant'].upper()} — {target_ver}",
        "redfish_multipart",
        curl_cmd,
        lambda job_id: _flash_cx8(bmc_ip, image_path, job_id=job_id, verify_version=target_ver),
        cleanup_path=cleanup_path,
        extra={"from_versions": current, "to_version": target_ver, "variant": target["variant"]},
    )
    return jsonify(
        {
            "ok": True,
            "job_id": job_id,
            "message": "Flash started — track progress in the side panel.",
            "from_versions": current,
            "to_version": target_ver,
            "variant": target["variant"],
            "target": target,
        }
    ), 202


def _load_os_inventory_map():
    """Load PowerCycling OS_INV expected lspci / nvme checks."""
    default = {"pcie": {"required": [], "min_devices": 0}, "nvme": {"required": [], "min_namespaces": 0}}
    try:
        with open(OS_INVENTORY_MAP_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data, ""
        return default, "os_inventory_map.json is not a JSON object."
    except FileNotFoundError:
        return default, f"Missing {OS_INVENTORY_MAP_FILE}"
    except (OSError, ValueError) as exc:
        return default, str(exc)


def _usb_golden_for_source(golden, source):
    """BMC and host USB trees are different; pick the matching view."""
    view_key = "bmc" if source == "bmc" else "os"
    views = golden.get("views") if isinstance(golden.get("views"), dict) else {}
    view = views.get(view_key)
    if not isinstance(view, dict):
        return golden
    merged = dict(golden)
    merged.update(view)
    merged["view_key"] = view_key
    return merged


def _load_usb_golden_map():
    """Load the schematic-derived USB golden configuration."""
    default = {
        "board": "MGX ARC HPM",
        "command": "lsusb -tv",
        "devices": [],
        "tree": [],
        "expectations": {"min_hubs": 1, "min_devices": 1},
        "notes": [],
    }
    try:
        with open(USB_GOLDEN_MAP_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data, ""
        return default, "usb_golden_map.json is not a JSON object."
    except FileNotFoundError:
        return default, f"Missing {USB_GOLDEN_MAP_FILE}"
    except (OSError, ValueError) as exc:
        return default, str(exc)


def _parse_lsusb_flat(text):
    """Parse `lsusb` lines into [{bus, device, vid, pid, name}]."""
    devices = []
    for line in (text or "").splitlines():
        match = re.search(
            r"Bus\s+(\d+)\s+Device\s+(\d+):\s+ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)$",
            line.strip(),
        )
        if not match:
            continue
        devices.append(
            {
                "bus": match.group(1),
                "device": match.group(2),
                "vid": match.group(3).lower(),
                "pid": match.group(4).lower(),
                "name": (match.group(5) or "").strip(),
                "raw": line.strip(),
            }
        )
    return devices


def _parse_lsusb_tree(text):
    """Parse `lsusb -tv` into a nested tree and a flat list of nodes."""
    root = {"name": "USB", "children": [], "raw": "", "level": -1}
    stack = [root]
    flat = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        # Tree lines usually start with "/:" or "|__" / "    |__"
        stripped = raw.replace("\t", "    ")
        stripped = re.sub(
            r"^unable to initialize usb spec", "", stripped, flags=re.I
        )
        if stripped.lstrip().startswith("/:"):
            level = 0
            name = stripped.lstrip()[2:].strip() or stripped.strip()
        elif "|__" in stripped or "+--" in stripped:
            idx = stripped.find("|__")
            if idx < 0:
                idx = stripped.find("+--")
            level = max(1, (idx // 4) + 1)
            name = stripped[idx + 3:].strip()
        else:
            # Continuation / Class / Driver lines attach to the last node.
            if flat:
                flat[-1]["detail"] = (
                    (flat[-1].get("detail") or "") + " " + stripped.strip()
                ).strip()
                flat[-1]["raw"] = (flat[-1].get("raw") or "") + "\n" + raw.rstrip()
            continue
        node = {
            "name": name,
            "children": [],
            "raw": raw.rstrip(),
            "detail": "",
            "level": level,
        }
        while len(stack) > level + 1:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
        flat.append(node)
    return root["children"], flat


def _usb_haystack(flat_devices, tree_nodes):
    """Single lowercase blob used for pattern matching."""
    parts = []
    for d in flat_devices:
        parts.append(d.get("raw") or "")
        parts.append(d.get("name") or "")
        parts.append(f"{d.get('vid', '')}:{d.get('pid', '')}")
    for n in tree_nodes:
        parts.append(n.get("name") or "")
        parts.append(n.get("detail") or "")
        parts.append(n.get("raw") or "")
    return "\n".join(parts).lower()


def _match_usb_device(entry, haystack, hub_count):
    """Return (status, detail) for one golden device entry."""
    patterns = [str(p).lower() for p in (entry.get("match") or []) if str(p).strip()]
    required = bool(entry.get("required", True))
    match_any = bool(entry.get("match_any", False))
    if not patterns:
        return "skip", "No match patterns configured."

    hits = [p for p in patterns if p in haystack]
    ok = bool(hits) if match_any else all(p in haystack for p in patterns)
    # Hub roles only need the Hub class somewhere; count is checked globally.
    if entry.get("id", "").startswith("hub") and "hub" in patterns:
        ok = hub_count > 0 and bool(hits)

    if ok:
        return "pass", f"Matched: {', '.join(hits) if hits else 'ok'}"
    if required:
        return "fail", f"Missing patterns: {', '.join(patterns)}"
    return "warn", f"Optional device not seen (looked for: {', '.join(patterns)})"


def _compare_usb_to_golden(golden, flat_devices, tree_nodes, tree_text):
    haystack = _usb_haystack(flat_devices, tree_nodes)
    hub_count = sum(
        1
        for n in tree_nodes
        if "hub" in f"{n.get('name', '')} {n.get('detail', '')}".lower()
    )
    if hub_count == 0:
        hub_count = sum(1 for d in flat_devices if "hub" in (d.get("name") or "").lower())

    entries = list(golden.get("devices") or [])
    required_hubs = [
        e for e in entries
        if bool(e.get("required", True)) and str(e.get("id", "")).startswith("hub")
    ]
    hubs_ok = hub_count >= max(len(required_hubs), 1) if required_hubs else True

    results = []
    fails = 0
    warns = 0
    for entry in entries:
        is_hub = str(entry.get("id", "")).startswith("hub")
        if is_hub and entry in required_hubs:
            if hubs_ok:
                status, detail = "pass", f"Counted {hub_count} hub(s) (≥ {len(required_hubs)} required)."
            else:
                status, detail = (
                    "fail",
                    f"Only {hub_count} hub(s) seen; schematic expects ≥ {len(required_hubs)} "
                    f"(Hub-0/1/2).",
                )
        else:
            status, detail = _match_usb_device(entry, haystack, hub_count)
        row = {
            "id": entry.get("id"),
            "role": entry.get("role"),
            "refdes": entry.get("refdes"),
            "schematic_page": entry.get("schematic_page"),
            "required": bool(entry.get("required", True)),
            "status": status,
            "detail": detail,
            "notes": entry.get("notes") or "",
        }
        results.append(row)
        if status == "fail":
            fails += 1
        elif status == "warn":
            warns += 1

    expectations = golden.get("expectations") or {}
    min_hubs = int(expectations.get("min_hubs") or 0)
    min_devices = int(expectations.get("min_devices") or 0)
    global_checks = []
    if min_hubs:
        hub_ok = hub_count >= min_hubs
        global_checks.append(
            {
                "id": "min_hubs",
                "role": f"At least {min_hubs} USB hubs",
                "status": "pass" if hub_ok else "fail",
                "detail": f"Saw {hub_count} hub(s).",
            }
        )
        if not hub_ok:
            fails += 1
    if min_devices:
        count = max(len(flat_devices), len(tree_nodes))
        dev_ok = count >= min_devices
        global_checks.append(
            {
                "id": "min_devices",
                "role": f"At least {min_devices} enumerated nodes",
                "status": "pass" if dev_ok else "fail",
                "detail": f"Saw {count} device/node(s).",
            }
        )
        if not dev_ok:
            fails += 1

    overall = "pass" if fails == 0 else "fail"
    summary = (
        f"{'PASS' if overall == 'pass' else 'FAIL'}: "
        f"{len(results) - fails - warns} matched, {warns} optional missing, {fails} failed. "
        f"Hubs={hub_count}, flat devices={len(flat_devices)}."
    )
    return {
        "overall": overall,
        "summary": summary,
        "hub_count": hub_count,
        "device_count": len(flat_devices),
        "results": global_checks + results,
        "tree_present": bool((tree_text or "").strip()),
    }


def _usb_enumerate_on_host(host_ip, *, username=None, password=None, source="bmc"):
    """Run lsusb / lsusb -tv on a host over SSH and compare to the golden map."""
    label = "BMC" if source == "bmc" else "OS host"
    golden, cfg_err = _load_usb_golden_map()
    golden = _usb_golden_for_source(golden, source)
    client, err = _ssh_connect(
        host_ip, username=username, password=password, label=label
    )
    if err:
        return False, err, None

    try:
        # Flat list + tree: flat helps VID:PID/name matching; tree is the live view.
        _code_flat, out_flat, err_flat = _ssh_run(
            client, "lsusb 2>/dev/null || true", timeout=USB_ENUM_TIMEOUT
        )
        _code_tv, out_tv, err_tv = _ssh_run(
            client, "lsusb -tv 2>/dev/null || lsusb -t 2>/dev/null || true",
            timeout=USB_ENUM_TIMEOUT,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    tree_text = (out_tv or "").strip() or (err_tv or "").strip()
    flat_text = (out_flat or "").strip() or (err_flat or "").strip()
    if not tree_text and not flat_text:
        return False, (
            f"lsusb produced no output on the {label}. Confirm usbutils is installed "
            f"and that the {label} USB host is up."
        ), None

    flat_devices = _parse_lsusb_flat(flat_text)
    live_tree, tree_nodes = _parse_lsusb_tree(tree_text)
    compare = _compare_usb_to_golden(golden, flat_devices, tree_nodes, tree_text)

    payload = {
        "source": source,
        "host_ip": host_ip,
        "bmc_ip": host_ip if source == "bmc" else "",
        "os_ip": host_ip if source == "os" else "",
        "command": "lsusb && lsusb -tv",
        "raw_lsusb": flat_text,
        "raw_tree": tree_text,
        "devices": flat_devices,
        "live_tree": live_tree,
        "golden": {
            "board": golden.get("board"),
            "schematic": golden.get("schematic"),
            "schematic_rev": golden.get("schematic_rev"),
            "view": golden.get("view"),
            "view_key": golden.get("view_key") or source,
            "notes": golden.get("notes") or [],
            "tree": golden.get("tree") or [],
            "config_error": cfg_err or "",
        },
        "compare": compare,
    }
    return True, "", payload


def _bmc_usb_enumerate(bmc_ip):
    """Run lsusb on the BMC and compare against the schematic golden map."""
    return _usb_enumerate_on_host(bmc_ip, source="bmc")


def _os_usb_enumerate(os_ip, username, password):
    """Run lsusb on the OS host IP and compare against the golden map."""
    return _usb_enumerate_on_host(
        os_ip, username=username, password=password, source="os"
    )


@app.route("/api/bmc/usb/enumerate", methods=["GET"])
def bmc_usb_enumerate():
    """USB Enum: lsusb -tv on the connected BMC vs schematic golden map."""
    session, err = _require_bmc_session()
    if err:
        return err
    ok, message, data = _bmc_usb_enumerate(session["bmc_ip"])
    if not ok:
        return jsonify({"error": message}), 502
    return jsonify(data)


@app.route("/api/bmc/usb/enumerate-os", methods=["POST"])
def bmc_usb_enumerate_os():
    """USB Enum: lsusb -tv on a user-supplied OS IP vs schematic golden map."""
    session, err = _require_bmc_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    os_ip = str(body.get("os_ip") or "").strip()
    username = str(body.get("username") or "").strip()
    password = body.get("password")
    if password is None:
        password = ""
    password = str(password)
    if not os_ip:
        return jsonify({"error": "OS IP address is required."}), 400
    if not username:
        return jsonify({"error": "OS SSH username is required."}), 400
    if not password:
        return jsonify({"error": "OS SSH password is required."}), 400
    # Prefer the OS IP from the form; fall back to session host IP if present.
    _ = session
    ok, message, data = _os_usb_enumerate(os_ip, username, password)
    if not ok:
        return jsonify({"error": message}), 502
    return jsonify(data)


@app.route("/api/bmc/usb/golden", methods=["GET"])
def bmc_usb_golden():
    """Return the golden USB mapping table (no BMC call)."""
    session, err = _require_bmc_session()
    if err:
        return err
    golden, cfg_err = _load_usb_golden_map()
    view = _usb_golden_for_source(golden, "bmc")
    return jsonify({"golden": view, "config_error": cfg_err or ""})


# ---------------------------------------------------------------------------
# PCIe — run lspci on host OS over SSH
# ---------------------------------------------------------------------------
_LSPCI_LINE_RE = re.compile(
    r"^([0-9a-fA-F:.]+)\s+([^:]+):\s+(.*)$"
)


def _parse_lspci(text):
    """Parse `lspci` output into a list of device dicts."""
    devices = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = _LSPCI_LINE_RE.match(line)
        if not match:
            devices.append({
                "address": "",
                "class": "",
                "description": line,
                "raw": line,
            })
            continue
        devices.append({
            "address": match.group(1),
            "class": match.group(2).strip(),
            "description": match.group(3).strip(),
            "raw": line,
        })
    return devices


def _inventory_haystack(rows):
    return "\n".join(
        f"{row.get('address', '')} {row.get('class', '')} {row.get('description', '')} {row.get('raw', '')}"
        for row in rows
    ).lower()


def _match_inventory_requirement(entry, haystack, row_count=None):
    patterns = [str(p).lower() for p in (entry.get("match") or []) if str(p).strip()]
    match_any = bool(entry.get("match_any", False))
    min_count = int(entry.get("min_count") or 1)
    if not patterns:
        return "skip", "No match patterns configured.", 0
    hits = [p for p in patterns if p in haystack]
    ok_presence = bool(hits) if match_any else all(p in haystack for p in patterns)
    count = 0
    if row_count is not None:
        count = row_count
    elif hits:
        # Approximate function count by how often the first hit string appears.
        count = haystack.count(hits[0])
    if not ok_presence or count < min_count:
        return (
            "fail",
            f"Need >= {min_count}; matched {', '.join(hits) or 'none'} (count~{count}).",
            count,
        )
    return "pass", f"Matched {', '.join(hits)} (count~{count}).", count


def _compare_os_inventory(expected, pcie_devices, nvme_text):
    pcie_cfg = expected.get("pcie") or {}
    nvme_cfg = expected.get("nvme") or {}
    haystack = _inventory_haystack(pcie_devices)
    results = []
    fails = 0
    min_devices = int(pcie_cfg.get("min_devices") or 0)
    if min_devices:
        ok = len(pcie_devices) >= min_devices
        results.append({
            "id": "min_pcie_devices",
            "role": f"At least {min_devices} lspci rows",
            "status": "pass" if ok else "fail",
            "detail": f"Saw {len(pcie_devices)} device(s).",
        })
        if not ok:
            fails += 1
    for entry in pcie_cfg.get("required") or []:
        # Count rows whose raw line contains any match token.
        patterns = [str(p).lower() for p in (entry.get("match") or []) if str(p).strip()]
        row_hits = 0
        if patterns:
            match_any = bool(entry.get("match_any", False))
            for dev in pcie_devices:
                blob = f"{dev.get('class', '')} {dev.get('description', '')} {dev.get('raw', '')}".lower()
                if match_any:
                    hit = any(p in blob for p in patterns)
                else:
                    hit = all(p in blob for p in patterns)
                if hit:
                    row_hits += 1
        status, detail, _count = _match_inventory_requirement(entry, haystack, row_hits)
        if status == "skip":
            continue
        results.append({
            "id": entry.get("id"),
            "role": entry.get("role") or entry.get("id"),
            "status": status,
            "detail": detail,
        })
        if status == "fail":
            fails += 1

    nvme_hay = (nvme_text or "").lower()
    min_ns = int(nvme_cfg.get("min_namespaces") or 0)
    nvme_rows = [
        line for line in (nvme_text or "").splitlines()
        if line.strip().startswith("/dev/nvme")
    ]
    if min_ns:
        ok = len(nvme_rows) >= min_ns
        results.append({
            "id": "min_nvme",
            "role": f"At least {min_ns} NVMe namespace(s)",
            "status": "pass" if ok else "fail",
            "detail": f"Saw {len(nvme_rows)} nvme device row(s).",
        })
        if not ok:
            fails += 1
    for entry in nvme_cfg.get("required") or []:
        status, detail, _count = _match_inventory_requirement(entry, nvme_hay, None)
        if status == "skip":
            continue
        results.append({
            "id": entry.get("id"),
            "role": entry.get("role") or entry.get("id"),
            "status": status,
            "detail": detail,
        })
        if status == "fail":
            fails += 1

    overall = "pass" if fails == 0 else "fail"
    return {
        "overall": overall,
        "summary": (
            f"{'PASS' if overall == 'pass' else 'FAIL'}: "
            f"{len(results) - fails} matched, {fails} failed (PowerCycling OS_INV)."
        ),
        "results": results,
    }


def _os_pcie_lspci(os_ip, username, password):
    """SSH to host OS and run PowerCycling OS_INV lspci + nvme --list."""
    expected, cfg_err = _load_os_inventory_map()
    client, err = _ssh_connect(
        os_ip, username=username, password=password, label="OS host"
    )
    if err:
        return False, err, None

    try:
        code, out, err_out = _ssh_run(
            client, "lspci 2>/dev/null || true", timeout=PCIE_LSPCI_TIMEOUT
        )
        _nvme_code, nvme_out, nvme_err = _ssh_run(
            client, "nvme --list 2>/dev/null || nvme list 2>/dev/null || true",
            timeout=PCIE_LSPCI_TIMEOUT,
        )
        combined = "\n".join(
            line
            for chunk in (out or "", err_out or "")
            for line in chunk.splitlines()
            if line.strip()
        ).strip()
        nvme_text = "\n".join(
            line
            for chunk in (nvme_out or "", nvme_err or "")
            for line in chunk.splitlines()
            if line.strip()
        ).strip()
        if not combined:
            return False, (
                "lspci produced no output on the OS host. Confirm pciutils is "
                "installed (apt install pciutils) and the IP is reachable."
            ), None
        devices = _parse_lspci(combined)
        compare = _compare_os_inventory(expected, devices, nvme_text)
        overall = compare.get("overall") or "fail"
        return True, compare.get("summary") or f"Found {len(devices)} PCIe device(s).", {
            "ok": overall == "pass",
            "os_ip": os_ip,
            "username": username,
            "command": "lspci && nvme --list",
            "flow": "PowerCycling OS_INV",
            "exit_code": code,
            "device_count": len(devices),
            "devices": devices,
            "raw": combined,
            "nvme_raw": nvme_text,
            "compare": compare,
            "config_error": cfg_err or "",
            "message": compare.get("summary") or f"Found {len(devices)} PCIe device(s).",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


@app.route("/api/bmc/pcie/lspci", methods=["POST"])
def bmc_pcie_lspci():
    """PCIe tab: SSH to OS IP and run PowerCycling OS_INV."""
    session, err = _require_bmc_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    os_ip = str(body.get("os_ip") or "").strip()
    username = str(body.get("username") or "aerial").strip() or "aerial"
    password = body.get("password")
    if password is None:
        password = ""
    password = str(password)
    if not os_ip:
        return jsonify({"error": "OS IP address is required."}), 400
    if not password:
        return jsonify({"error": "OS SSH password is required."}), 400
    _ = session
    ok, message, data = _os_pcie_lspci(os_ip, username, password)
    if not ok:
        return jsonify({"error": message}), 502
    return jsonify(data)


# ---------------------------------------------------------------------------
# KVM — hand off to the BMC web UI's KVM console
# ---------------------------------------------------------------------------
# webui-vue serves the KVM viewer twice: full-screen at /#/console/kvm (what its
# own "Open in new tab" uses) and inside the Operations nav at /#/operations/kvm.
KVM_CONSOLE_PATH = "/#/console/kvm"
KVM_OPERATIONS_PATH = "/#/operations/kvm"


def _bmc_web_up(bmc_ip):
    """Confirm the BMC web UI answers on HTTPS, so we don't open a dead tab."""
    try:
        resp = _redfish_session().get(
            f"https://{bmc_ip}/",
            verify=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True,
        )
    except requests.exceptions.ConnectTimeout:
        return False, f"Timed out connecting to https://{bmc_ip}."
    except requests.exceptions.SSLError as exc:
        # A TLS complaint still proves something is listening on 443.
        return True, f"BMC web UI reachable (self-signed cert: {exc.__class__.__name__})."
    except requests.exceptions.ConnectionError:
        return False, f"Could not reach https://{bmc_ip} on the network."
    except requests.exceptions.RequestException as exc:
        return False, f"Request to https://{bmc_ip} failed: {exc}"
    return True, f"BMC web UI responded (HTTP {resp.status_code})."


@app.route("/api/bmc/kvm/target", methods=["POST"])
def bmc_kvm_target():
    """KVM tab: verify the BMC web UI is up and return its console URLs."""
    session, err = _require_bmc_session()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    bmc_ip = str(body.get("bmc_ip") or "").strip() or session["bmc_ip"]
    if not bmc_ip:
        return jsonify({"error": "BMC address is required."}), 400

    up, message = _bmc_web_up(bmc_ip)
    if not up:
        return jsonify({"error": message}), 502

    # /Managers requires auth on bmcweb, so a success here means root/0penBmc
    # will get through the web UI login too.
    redfish_ok, redfish_detail, _ = _redfish_get(bmc_ip, "/Managers")
    return jsonify({
        "ok": True,
        "bmc_ip": bmc_ip,
        "console_url": f"https://{bmc_ip}{KVM_CONSOLE_PATH}",
        "operations_url": f"https://{bmc_ip}{KVM_OPERATIONS_PATH}",
        "home_url": f"https://{bmc_ip}/",
        "username": REQUIRED_BMC_USER,
        "credentials_valid": bool(redfish_ok),
        "credentials_detail": (
            "root / 0penBmc accepted by the BMC."
            if redfish_ok else str(redfish_detail)
        ),
        "message": message,
    })


# ---------------------------------------------------------------------------
# Network oscilloscope (SCPI over TCP) — live virtual scope view
# ---------------------------------------------------------------------------
_SCOPE_LOCK = threading.Lock()
_SCOPE_SESSIONS = {}  # scope_ip -> {sock, lock, idn, vendor, port, connected_at}


class _ScopeScpi:
    """Minimal SCPI-over-TCP client (IEEE 488.2 definite-length blocks)."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass

    def write(self, cmd):
        payload = (cmd.rstrip("\n") + "\n").encode("ascii", "replace")
        self.sock.sendall(payload)

    def _recv_until(self, needle=b"\n", max_bytes=8 * 1024 * 1024):
        while needle not in self.buf and len(self.buf) < max_bytes:
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            self.buf.extend(chunk)
        idx = self.buf.find(needle)
        if idx < 0:
            data = bytes(self.buf)
            self.buf.clear()
            return data
        data = bytes(self.buf[: idx + len(needle)])
        del self.buf[: idx + len(needle)]
        return data

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                break
            self.buf.extend(chunk)
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def query(self, cmd, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        self.write(cmd)
        raw = self._recv_until(b"\n")
        return raw.decode("utf-8", "replace").strip()

    def query_binary(self, cmd, timeout=None):
        """Read an IEEE 488.2 definite-length binary block (#NXXXX<data>)."""
        if timeout is not None:
            self.sock.settimeout(timeout)
        self.write(cmd)
        # Some instruments prepend a newline; skip whitespace until '#'.
        while True:
            if self.buf:
                # Drop leading whitespace / newlines before the block header.
                while self.buf and self.buf[0] in (0x0A, 0x0D, 0x20, 0x09):
                    del self.buf[0]
                if self.buf and self.buf[0] == ord("#"):
                    break
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("Scope closed the connection while reading binary data.")
            self.buf.extend(chunk)

        header = self._recv_exact(2)  # '#N'
        if len(header) < 2 or header[0:1] != b"#":
            raise OSError("Scope returned a non-binary response (missing # header).")
        ndigits = header[1:2]
        if not ndigits.isdigit():
            # Indefinite block (#0) — read until NL; uncommon for screenshots.
            rest = self._recv_until(b"\n")
            return rest.rstrip(b"\r\n")
        n = int(ndigits.decode("ascii"))
        len_bytes = self._recv_exact(n)
        try:
            payload_len = int(len_bytes.decode("ascii"))
        except ValueError as exc:
            raise OSError(f"Bad IEEE block length: {len_bytes!r}") from exc
        data = self._recv_exact(payload_len)
        # Trailing newline is common; drain it if present without blocking forever.
        try:
            self.sock.settimeout(0.2)
            trail = self.sock.recv(8)
            if trail and trail not in (b"\n", b"\r\n", b"\r"):
                self.buf.extend(trail.lstrip(b"\r\n"))
        except (OSError, socket.timeout):
            pass
        finally:
            self.sock.settimeout(SCOPE_IO_TIMEOUT)
        return data


def _scope_vendor_from_idn(idn):
    text = (idn or "").upper()
    if "TEKTRONIX" in text or text.startswith("TEK"):
        return "tektronix"
    if "KEYSIGHT" in text or "AGILENT" in text:
        return "keysight"
    if "RIGOL" in text:
        return "rigol"
    if "ROHDE" in text or "R&S" in text or "RSU" in text:
        return "rohde"
    if "LECROY" in text or "TELEDYNE" in text:
        return "lecroy"
    return "generic"


def _scope_open_socket(scope_ip, port):
    sock = socket.create_connection(
        (scope_ip, int(port)), timeout=SCOPE_CONNECT_TIMEOUT
    )
    sock.settimeout(SCOPE_IO_TIMEOUT)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    return sock


def _scope_try_connect(scope_ip, preferred_port=None):
    ports = []
    if preferred_port:
        ports.append(int(preferred_port))
    for p in SCOPE_SCPI_PORTS:
        if p not in ports:
            ports.append(p)

    errors = []
    for port in ports:
        try:
            sock = _scope_open_socket(scope_ip, port)
            client = _ScopeScpi(sock)
            # Clear any greeting, then identify.
            try:
                sock.settimeout(0.3)
                junk = sock.recv(4096)
                if junk:
                    client.buf.extend(junk)
            except (OSError, socket.timeout):
                pass
            finally:
                sock.settimeout(SCOPE_IO_TIMEOUT)
            try:
                client.write("*CLS")
            except OSError:
                pass
            idn = client.query("*IDN?", timeout=SCOPE_IO_TIMEOUT)
            if not idn:
                client.close()
                errors.append(f"port {port}: empty *IDN?")
                continue
            return {
                "sock_client": client,
                "port": port,
                "idn": idn,
                "vendor": _scope_vendor_from_idn(idn),
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"port {port}: {exc}")
    detail = "; ".join(errors[:4]) if errors else "no ports tried"
    return None, (
        f"Could not open SCPI on {scope_ip} "
        f"(tried {', '.join(str(p) for p in ports)}). {detail}"
    )


def _scope_session_get(scope_ip):
    with _SCOPE_LOCK:
        return _SCOPE_SESSIONS.get(scope_ip)


def _scope_session_set(scope_ip, session):
    with _SCOPE_LOCK:
        old = _SCOPE_SESSIONS.pop(scope_ip, None)
        _SCOPE_SESSIONS[scope_ip] = session
    if old and old.get("sock_client") and old["sock_client"] is not session.get("sock_client"):
        try:
            old["sock_client"].close()
        except Exception:
            pass


def _scope_session_drop(scope_ip):
    with _SCOPE_LOCK:
        old = _SCOPE_SESSIONS.pop(scope_ip, None)
    if old and old.get("sock_client"):
        try:
            old["sock_client"].close()
        except Exception:
            pass


def _scope_ensure_session(scope_ip, port=None):
    existing = _scope_session_get(scope_ip)
    if existing and existing.get("sock_client"):
        # Health-check with a cheap query; reconnect on failure.
        try:
            with existing["lock"]:
                idn = existing["sock_client"].query("*IDN?", timeout=3)
            if idn:
                existing["idn"] = idn
                existing["vendor"] = _scope_vendor_from_idn(idn)
                return existing, ""
        except Exception:
            _scope_session_drop(scope_ip)

    result = _scope_try_connect(scope_ip, preferred_port=port)
    if isinstance(result, tuple):
        return None, result[1]
    session = {
        "sock_client": result["sock_client"],
        "port": result["port"],
        "idn": result["idn"],
        "vendor": result["vendor"],
        "lock": threading.Lock(),
        "connected_at": time.time(),
        "scope_ip": scope_ip,
    }
    _scope_session_set(scope_ip, session)
    return session, ""


def _scope_safe_query(client, cmd, default=""):
    try:
        return client.query(cmd, timeout=SCOPE_IO_TIMEOUT)
    except Exception:
        return default


def _scope_grab_screenshot(client, vendor):
    """Return (png_bytes, note). Empty bytes if unsupported."""
    commands = []
    if vendor == "keysight":
        commands = [
            ":HARDcopy:INKSaver OFF",
            ":DISPlay:DATA? PNG,COLOR",
            ":DISPlay:DATA? PNG, COLor",
            ":DISP:DATA? PNG",
        ]
    elif vendor == "tektronix":
        commands = [
            "DISplay:DATa? PNG",
            "HARDCopy:FORMat PNG",
            ":DISPlay:DATA? PNG",
        ]
    elif vendor == "rigol":
        commands = [":DISP:DATA? PNG", ":DISPLAY:DATA? PNG"]
    else:
        commands = [
            ":DISPlay:DATA? PNG,COLOR",
            ":DISPlay:DATA? PNG",
            "DISplay:DATa? PNG",
            ":DISP:DATA? PNG",
        ]

    last_err = ""
    for cmd in commands:
        if not cmd.endswith("?"):
            try:
                client.write(cmd)
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
            continue
        try:
            data = client.query_binary(cmd, timeout=max(SCOPE_IO_TIMEOUT, 20))
            if data and len(data) > 100 and data[:8].startswith(b"\x89PNG"):
                return data, cmd
            # Some scopes return BMP/JPEG — still usable in <img> if we sniff.
            if data and len(data) > 100:
                return data, cmd
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
    return b"", last_err or "Screenshot SCPI not supported on this scope."


def _scope_parse_keysight_preamble(preamble):
    # format, type, points, count, xinc, xorg, xref, yinc, yorg, yref
    parts = [p.strip() for p in (preamble or "").split(",")]
    if len(parts) < 10:
        return None
    try:
        return {
            "points": int(float(parts[2])),
            "x_inc": float(parts[4]),
            "x_origin": float(parts[5]),
            "x_reference": float(parts[6]),
            "y_inc": float(parts[7]),
            "y_origin": float(parts[8]),
            "y_reference": float(parts[9]),
        }
    except ValueError:
        return None


def _scope_fetch_channel_keysight(client, ch):
    src = f"CHANnel{ch}"
    try:
        displayed = _scope_safe_query(client, f":CHANnel{ch}:DISPlay?", "0")
        if displayed.strip() in ("0", "OFF", "off"):
            return None
        client.write(f":WAVeform:SOURce {src}")
        client.write(":WAVeform:FORMat BYTE")
        client.write(":WAVeform:POINts:MODE NORMal")
        client.write(f":WAVeform:POINts {SCOPE_WAVE_POINTS}")
        preamble = client.query(":WAVeform:PREamble?")
        meta = _scope_parse_keysight_preamble(preamble)
        raw = client.query_binary(":WAVeform:DATA?", timeout=SCOPE_IO_TIMEOUT)
        if not raw or not meta:
            return None
        y_inc = meta["y_inc"]
        y_org = meta["y_origin"]
        y_ref = meta["y_reference"]
        volts = [((b - y_ref) * y_inc) + y_org for b in raw]
        step = 1
        if len(volts) > 2000:
            step = max(1, len(volts) // 1000)
            volts = volts[::step]
        x_inc = meta["x_inc"]
        x_org = meta["x_origin"]
        times = [x_org + (i * step) * x_inc for i in range(len(volts))]
        return {
            "channel": ch,
            "label": f"CH{ch}",
            "volts": volts,
            "times": times,
            "v_min": min(volts) if volts else 0,
            "v_max": max(volts) if volts else 0,
        }
    except Exception:
        return None


def _scope_fetch_channel_tek(client, ch):
    try:
        client.write("HEADER OFF")
        client.write(f"DATa:SOUrce CH{ch}")
        client.write("DATa:ENCdg RIBINARY")
        client.write("DATa:WIDth 1")
        client.write("DATa:STARt 1")
        client.write(f"DATa:STOP {SCOPE_WAVE_POINTS}")
        # Probe if channel exists by asking for ymult; failures mean skip.
        ymult = _scope_safe_query(client, "WFMOutpre:YMUlt?", "")
        if not ymult:
            return None
        yoff = float(_scope_safe_query(client, "WFMOutpre:YOFF?", "0") or 0)
        yzero = float(_scope_safe_query(client, "WFMOutpre:YZEro?", "0") or 0)
        xincr = float(_scope_safe_query(client, "WFMOutpre:XINcr?", "1e-9") or 1e-9)
        xzero = float(_scope_safe_query(client, "WFMOutpre:XZEro?", "0") or 0)
        ymult_f = float(ymult)
        raw = client.query_binary("CURVe?", timeout=SCOPE_IO_TIMEOUT)
        if not raw:
            return None
        # RIBINARY is signed 8-bit
        samples = list(struct.unpack(f">{len(raw)}b", raw))
        volts = [((s - yoff) * ymult_f) + yzero for s in samples]
        if len(volts) > 2000:
            step = max(1, len(volts) // 1000)
            volts = volts[::step]
        else:
            step = 1
        times = [xzero + i * xincr * step for i in range(len(volts))]
        return {
            "channel": ch,
            "label": f"CH{ch}",
            "volts": volts,
            "times": times,
            "v_min": min(volts) if volts else 0,
            "v_max": max(volts) if volts else 0,
        }
    except Exception:
        return None


def _scope_fetch_waveforms(client, vendor):
    waves = []
    for ch in range(1, 5):
        if vendor == "tektronix":
            wave = _scope_fetch_channel_tek(client, ch)
        else:
            # Keysight / Rigol / generic — try Keysight-style first.
            wave = _scope_fetch_channel_keysight(client, ch)
            if wave is None and vendor == "tektronix":
                wave = _scope_fetch_channel_tek(client, ch)
        if wave:
            waves.append(wave)
    return waves


def _scope_fetch_measurements(client, vendor):
    """Best-effort voltage/frequency readouts for channels 1–4."""
    rows = []
    for ch in range(1, 5):
        entry = {"channel": ch, "label": f"CH{ch}", "vpp": None, "freq": None, "vrms": None}
        if vendor == "tektronix":
            try:
                client.write(f"MEASUrement:IMMed:SOUrce CH{ch}")
                client.write("MEASUrement:IMMed:TYPe PK2Pk")
                vpp = _scope_safe_query(client, "MEASUrement:IMMed:VALue?", "")
                client.write("MEASUrement:IMMed:TYPe FREQuency")
                freq = _scope_safe_query(client, "MEASUrement:IMMed:VALue?", "")
                entry["vpp"] = vpp
                entry["freq"] = freq
            except Exception:
                pass
        else:
            entry["vpp"] = _scope_safe_query(client, f":MEASure:VPP? CHANnel{ch}", "") or \
                _scope_safe_query(client, f":MEASure:VPP? CHAN{ch}", "")
            entry["freq"] = _scope_safe_query(client, f":MEASure:FREQuency? CHANnel{ch}", "") or \
                _scope_safe_query(client, f":MEASure:FREQ? CHAN{ch}", "")
            entry["vrms"] = _scope_safe_query(client, f":MEASure:VRMS? CHANnel{ch}", "") or \
                _scope_safe_query(client, f":MEASure:VRMS? CHAN{ch}", "")
        # Drop empty channels.
        if any(entry.get(k) not in (None, "", "9.9E+37", "+9.9E+37") for k in ("vpp", "freq", "vrms")):
            # Sanitize overflow sentinels.
            for k in ("vpp", "freq", "vrms"):
                val = entry.get(k)
                if val in (None, "", "9.9E+37", "+9.9E+37", "9.91E+37"):
                    entry[k] = None
            rows.append(entry)
    return rows


def _scope_live_frame(session, want_screenshot=True, want_waveforms=True):
    client = session["sock_client"]
    vendor = session.get("vendor") or "generic"
    with session["lock"]:
        # Refresh identity lightly.
        idn = _scope_safe_query(client, "*IDN?", session.get("idn") or "")
        if idn:
            session["idn"] = idn
            session["vendor"] = _scope_vendor_from_idn(idn)
            vendor = session["vendor"]

        screenshot_b64 = ""
        screenshot_note = ""
        screenshot_mime = "image/png"
        if want_screenshot:
            png, note = _scope_grab_screenshot(client, vendor)
            screenshot_note = note
            if png:
                if png[:2] == b"\xff\xd8":
                    screenshot_mime = "image/jpeg"
                elif png[:2] == b"BM":
                    screenshot_mime = "image/bmp"
                screenshot_b64 = b64encode(png).decode("ascii")

        waveforms = _scope_fetch_waveforms(client, vendor) if want_waveforms else []
        measurements = _scope_fetch_measurements(client, vendor)

    return {
        "scope_ip": session["scope_ip"],
        "port": session["port"],
        "idn": session.get("idn") or "",
        "vendor": vendor,
        "screenshot": screenshot_b64,
        "screenshot_mime": screenshot_mime,
        "screenshot_note": screenshot_note,
        "waveforms": waveforms,
        "measurements": measurements,
        "ts": time.time(),
    }


def _require_scope_body_ip():
    body = request.get_json(silent=True) or {}
    scope_ip = str(
        body.get("scope_ip")
        or request.args.get("scope_ip")
        or request.headers.get("X-Scope-Host")
        or ""
    ).strip()
    port_raw = body.get("port") or request.args.get("port") or ""
    port = None
    if str(port_raw).strip().isdigit():
        port = int(str(port_raw).strip())
    return scope_ip, port, body


def _scope_vnc_resolve_target(host, port):
    """Validate and resolve a scope host without invoking a shell."""
    host = str(host or "").strip().strip("[]")
    if not host or len(host) > 253:
        raise ValueError("A valid scope IP address or hostname is required.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip(".").split(".")
        if not labels or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("Scope host must be a valid IP address or DNS hostname.")

    if port < 5900 or port > 5999:
        raise ValueError("VNC port must be between 5900 and 5999.")

    try:
        candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve scope host: {exc}") from exc
    if not candidates:
        raise ValueError("Could not resolve scope host.")

    allowed = []
    for family, socktype, proto, _canonname, sockaddr in candidates:
        address = ipaddress.ip_address(sockaddr[0])
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            continue
        allowed.append((family, socktype, proto, sockaddr))
    if not allowed:
        raise ValueError("The scope host resolves only to a disallowed address.")
    return allowed[0]


@app.route("/api/bmc/scope/vnc/session", methods=["POST"])
def scope_vnc_session():
    """Issue a short-lived, one-use token for the browser VNC relay."""
    _session_bmc, err = _require_bmc_session()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    host = str(body.get("scope_ip") or "").strip()
    try:
        port = int(body.get("port") or SCOPE_VNC_PORT)
        target = _scope_vnc_resolve_target(host, port)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    now = time.time()
    token = secrets.token_urlsafe(32)
    with _scope_vnc_tokens_lock:
        for old_token, item in list(_scope_vnc_tokens.items()):
            if item["expires"] <= now:
                _scope_vnc_tokens.pop(old_token, None)
        _scope_vnc_tokens[token] = {
            "target": target,
            "expires": now + SCOPE_VNC_TOKEN_TTL,
        }
    return jsonify({
        "ok": True,
        "token": token,
        "path": "/api/bmc/scope/vnc",
        "expires_in": SCOPE_VNC_TOKEN_TTL,
    })


@sock.route("/api/bmc/scope/vnc")
def scope_vnc_proxy(ws):
    """Relay binary WebSocket frames to one validated raw RFB endpoint."""
    token = str(request.args.get("token") or "")
    with _scope_vnc_tokens_lock:
        item = _scope_vnc_tokens.pop(token, None)
    if not item or item["expires"] <= time.time():
        ws.close(reason=1008, message="Invalid or expired VNC session.")
        return

    family, socktype, proto, sockaddr = item["target"]
    target = socket.socket(family, socktype, proto)
    stopped = threading.Event()

    def target_to_browser():
        try:
            while not stopped.is_set():
                try:
                    data = target.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                ws.send(data)
        except (OSError, ConnectionError):
            pass
        finally:
            stopped.set()

    try:
        target.settimeout(SCOPE_CONNECT_TIMEOUT)
        target.connect(sockaddr)
        target.settimeout(1.0)
        reader = threading.Thread(target=target_to_browser, daemon=True)
        reader.start()
        while not stopped.is_set():
            try:
                message = ws.receive(timeout=1)
            except TimeoutError:
                continue
            if message is None:
                break
            if not isinstance(message, (bytes, bytearray)):
                break
            target.sendall(message)
    except (OSError, ConnectionError):
        pass
    finally:
        stopped.set()
        try:
            target.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        target.close()


@app.route("/api/scope/connect", methods=["POST"])
def scope_connect():
    """Open a SCPI session to the scope IP and return identity."""
    session_bmc, err = _require_bmc_session()
    if err:
        return err
    _ = session_bmc
    scope_ip, port, _body = _require_scope_body_ip()
    if not scope_ip:
        return jsonify({"error": "Scope IP address is required."}), 400
    session, message = _scope_ensure_session(scope_ip, port=port)
    if not session:
        return jsonify({"error": message}), 502
    return jsonify({
        "ok": True,
        "scope_ip": scope_ip,
        "port": session["port"],
        "idn": session.get("idn") or "",
        "vendor": session.get("vendor") or "generic",
        "message": f"Connected to {session.get('idn') or scope_ip} on TCP {session['port']}.",
    })


@app.route("/api/scope/live", methods=["GET", "POST"])
def scope_live():
    """Return one live frame: screenshot (if available), waveforms, measurements."""
    session_bmc, err = _require_bmc_session()
    if err:
        return err
    _ = session_bmc
    scope_ip, port, body = _require_scope_body_ip()
    if not scope_ip:
        return jsonify({"error": "Scope IP address is required."}), 400
    want_shot = str(body.get("screenshot", request.args.get("screenshot", "1"))).lower() not in (
        "0", "false", "no"
    )
    want_wave = str(body.get("waveforms", request.args.get("waveforms", "1"))).lower() not in (
        "0", "false", "no"
    )
    session, message = _scope_ensure_session(scope_ip, port=port)
    if not session:
        return jsonify({"error": message}), 502
    try:
        frame = _scope_live_frame(
            session, want_screenshot=want_shot, want_waveforms=want_wave
        )
    except Exception as exc:  # noqa: BLE001
        _scope_session_drop(scope_ip)
        return jsonify({"error": f"Scope live read failed: {exc}"}), 502
    return jsonify(frame)


@app.route("/api/scope/disconnect", methods=["POST"])
def scope_disconnect():
    session_bmc, err = _require_bmc_session()
    if err:
        return err
    _ = session_bmc
    scope_ip, _port, _body = _require_scope_body_ip()
    if not scope_ip:
        return jsonify({"error": "Scope IP address is required."}), 400
    _scope_session_drop(scope_ip)
    return jsonify({"ok": True, "message": f"Disconnected from {scope_ip}."})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "4282"))
    print(f" * MGX ARC GUI running at http://0.0.0.0:{port}")
    # threaded=True so a long firmware upload doesn't block other requests
    # (e.g. re-checking versions in another tab while a flash runs).
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
