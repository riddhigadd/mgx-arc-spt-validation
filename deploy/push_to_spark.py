#!/usr/bin/env python3
"""Push changed GUI files to the Spark host and restart the service.

Credentials come from the environment so they are never written to disk:

    SPARK_HOST      target IP (default 10.110.33.21)
    SPARK_USER      SSH username
    SPARK_PASSWORD  SSH password
    SPARK_APP_DIR   install path (default /home/<user>/MGXARC-GUI-RIDDHI)

The install directory is owned by the service user, so files are copied
without sudo; only `systemctl restart` escalates. Replaced files are backed
up under deploy_backup/<timestamp>/ because the remote copy is not a git repo.

Usage:  python deploy/push_to_spark.py [file ...]
"""

from __future__ import annotations

import os
import posixpath
import sys
from datetime import datetime
from pathlib import Path

import paramiko

DEFAULT_FILES = (
    "app.py",
    "requirements.txt",
    # Every front-end asset ships together; pushing app.js alone leaves the
    # remote index.html stale and silently hides newer tabs.
    "static/index.html",
    "static/js/app.js",
    "static/js/scope-vnc.js",
    "static/vendor/novnc",
    "static/js/health-console.js",
    "static/css/style.css",
    "static/css/health-console.css",
    "backend/config.py",
    "backend/services/usb.py",
    "usb_golden_map.json",
    "i2c_bus_map.json",
    "i2c_expected_devices.json",
    "os_inventory_map.json",
    "config/mgx_arc/profiles.yaml",
    "config/mgx_arc/README.md",
)

ROOT = Path(__file__).resolve().parent.parent
SERVICE = "mgx-arc-gui"


def run(client: paramiko.SSHClient, command: str, password: str) -> tuple[int, str]:
    """Run a remote command, feeding the sudo prompt when one is expected.

    Uses `sudo -S -p ''` on a plain channel rather than a TTY so the password
    is never echoed back into the output.
    """
    sudo = command.startswith("sudo ")
    if sudo:
        command = command.replace("sudo ", "sudo -S -p '' ", 1)
    stdin, stdout, stderr = client.exec_command(command, timeout=180)
    if sudo:
        stdin.write(password + "\n")
        stdin.flush()
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def ensure_remote_dir(
    client: paramiko.SSHClient, app_dir: str, remote_dir: str, password: str
) -> None:
    """Create `remote_dir` and make sure the owner may write into it.

    Some directories in the install tree are mode dr-x------, so creating a
    new file there fails even though the SSH user owns them. The owner can
    restore the write bit without sudo, so no escalation is needed.
    """
    rel = posixpath.relpath(remote_dir, app_dir)
    parts = [] if rel in (".", "") else rel.split("/")
    path = app_dir
    run(client, f"chmod u+rwx {path}", password)
    for part in parts:
        path = posixpath.join(path, part)
        run(client, f"mkdir -p {path} && chmod u+rwx {path}", password)


def main() -> int:
    host = os.environ.get("SPARK_HOST", "10.110.33.21")
    user = os.environ.get("SPARK_USER", "")
    password = os.environ.get("SPARK_PASSWORD", "")
    if not user or not password:
        print("Set SPARK_USER and SPARK_PASSWORD first.", file=sys.stderr)
        return 2
    app_dir = os.environ.get("SPARK_APP_DIR", f"/home/{user}/MGXARC-GUI-RIDDHI")

    requested = sys.argv[1:] or list(DEFAULT_FILES)
    files = []
    for rel in requested:
        local = ROOT / rel
        if local.is_dir():
            files.extend(
                path.relative_to(ROOT).as_posix()
                for path in sorted(local.rglob("*"))
                if path.is_file()
            )
        else:
            files.append(rel)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = posixpath.join(app_dir, "deploy_backup", stamp)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=30)
    try:
        code, out = run(client, f"test -w {app_dir} && echo ok", password)
        if code != 0:
            print(f"{app_dir} is not writable by {user}: {out}", file=sys.stderr)
            return 1

        sftp = client.open_sftp()
        pushed = 0
        try:
            for rel in files:
                local = ROOT / rel
                if not local.is_file():
                    print(f"skip (missing locally): {rel}")
                    continue
                remote = posixpath.join(app_dir, rel)
                ensure_remote_dir(client, app_dir, posixpath.dirname(remote), password)
                ensure_remote_dir(
                    client, app_dir, posixpath.dirname(posixpath.join(backup, rel)), password
                )
                run(
                    client,
                    f"test -f {remote} && cp -a {remote} {posixpath.join(backup, rel)}",
                    password,
                )
                sftp.put(str(local), remote)
                print(f"pushed {rel} ({local.stat().st_size} bytes)")
                pushed += 1
        finally:
            sftp.close()
        print(f"\n{pushed} file(s) pushed; backups in {backup}")

        if "requirements.txt" in files:
            install = (
                f"if [ -x {app_dir}/venv/bin/python ]; then "
                f"{app_dir}/venv/bin/python -m pip install -r {app_dir}/requirements.txt; "
                f"elif [ -x {app_dir}/.venv/bin/python ]; then "
                f"{app_dir}/.venv/bin/python -m pip install -r {app_dir}/requirements.txt; "
                f"else python3 -m pip install --user -r {app_dir}/requirements.txt; fi"
            )
            code, output = run(client, install, password)
            if output:
                print(f"$ install Python requirements\n{output}")
            if code != 0:
                print(f"FAILED to install requirements (exit {code})", file=sys.stderr)
                return 1

        for step in (
            f"sudo systemctl restart {SERVICE}",
            "sleep 3",
            f"systemctl is-active {SERVICE}",
        ):
            code, output = run(client, step, password)
            if output:
                print(f"$ {step}\n{output}")
            if code != 0 and "is-active" not in step:
                print(f"FAILED (exit {code})", file=sys.stderr)
                return 1
    finally:
        client.close()
    print("\nDone. Hard-refresh the browser (Ctrl+F5).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
