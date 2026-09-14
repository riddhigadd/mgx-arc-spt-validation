"""MCTP endpoint collection and normalization."""

from __future__ import annotations

import re
from typing import Any

from backend.models import SnapshotKind
from backend.services.connection import ServiceContext, ServiceError, compare_rows, new_snapshot

EID_RE = re.compile(r"(?:eid\s+)?(?P<eid>\d+)(?:\s+net\s+(?P<network>\d+))?", re.I)


def parse_endpoints(output: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in output.splitlines():
        text = line.strip()
        if not text or text.lower().startswith(("eid ", "endpoint")):
            continue
        match = EID_RE.search(text)
        if not match:
            continue
        eid = match.group("eid")
        network = match.group("network") or "0"
        items.append(
            {
                "source": "bmc",
                "kind": "mctp_endpoint",
                "key": f"{network}:{eid}",
                "eid": int(eid),
                "network": int(network),
                "label": text,
            }
        )
    return items


def collect(ctx: ServiceContext, target_id: str):
    target = ctx.target(target_id)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or not capability.mctp:
        raise ServiceError("not_configured", 424)
    command = ctx.command(target, "mctp")
    result = ctx.provider.run_bmc_command(target, command)
    if not result.ok:
        raise ServiceError(result.error or "mctp_collection_failed", 502)
    items = parse_endpoints(result.stdout)
    snapshot = new_snapshot(
        target.id,
        SnapshotKind.MCTP,
        raw={"stdout": result.stdout, "stderr": result.stderr},
        normalized={"items": items, "count": len(items)},
    )
    return ctx.store.save_snapshot(snapshot)


def compare(ctx: ServiceContext, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = ctx.store.get_snapshot(first_id), ctx.store.get_snapshot(second_id)
    if not first or not second:
        raise ServiceError("snapshot_not_found", 404)
    if first.kind != SnapshotKind.MCTP or second.kind != SnapshotKind.MCTP:
        raise ServiceError("snapshot_kind_mismatch", 400)
    return compare_rows(first, second)
