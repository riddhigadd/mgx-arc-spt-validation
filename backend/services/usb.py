"""USB collection and normalization."""

from __future__ import annotations

import re
from typing import Any

from backend.models import SnapshotKind, to_plain
from backend.services.connection import ServiceContext, ServiceError, compare_rows, new_snapshot

LSUSB_RE = re.compile(
    r"Bus\s+(?P<bus>\d+)\s+Device\s+(?P<device>\d+):\s+ID\s+"
    r"(?P<vid>[0-9a-fA-F]{4}):(?P<pid>[0-9a-fA-F]{4})\s*(?P<label>.*)$"
)


def parse_lsusb(output: str, source: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = LSUSB_RE.search(line.strip())
        if not match:
            continue
        row = match.groupdict()
        key = f"{source}:{row['bus']}:{row['device']}:{row['vid'].lower()}:{row['pid'].lower()}"
        items.append(
            {
                "source": source,
                "kind": "usb",
                "key": key,
                "bus": row["bus"],
                "device": row["device"],
                "vid": row["vid"].lower(),
                "pid": row["pid"].lower(),
                "label": row["label"].strip(),
            }
        )
    return items


def collect(ctx: ServiceContext, target_id: str, source: str = "bmc"):
    target = ctx.target(target_id)
    if source not in {"bmc", "host"}:
        raise ServiceError("invalid_source", 400)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or source not in capability.usb_sources:
        raise ServiceError("source_not_supported", 409)
    command = ctx.command(target, f"usb.{source}")
    result = (
        ctx.provider.run_bmc_command(target, command)
        if source == "bmc"
        else ctx.provider.run_host_command(target, command)
    )
    if not result.ok:
        raise ServiceError(result.error or "usb_collection_failed", 502)
    items = parse_lsusb(result.stdout, source)
    snapshot = new_snapshot(
        target.id,
        SnapshotKind.USB,
        raw={"source": source, "stdout": result.stdout, "stderr": result.stderr},
        normalized={"items": items, "count": len(items), "source": source},
    )
    ctx.store.save_snapshot(snapshot)
    return snapshot


def compare(ctx: ServiceContext, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = ctx.store.get_snapshot(first_id), ctx.store.get_snapshot(second_id)
    if not first or not second:
        raise ServiceError("snapshot_not_found", 404)
    if first.kind != SnapshotKind.USB or second.kind != SnapshotKind.USB:
        raise ServiceError("snapshot_kind_mismatch", 400)
    return compare_rows(first, second)
