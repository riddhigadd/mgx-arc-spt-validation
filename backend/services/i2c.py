"""I2C health collection and i2cdetect parsing."""

from __future__ import annotations

import re
from typing import Any

from backend.models import SnapshotKind
from backend.services.connection import ServiceContext, ServiceError, new_snapshot

BUS_RE = re.compile(r"^(?:bus\s+)?(?P<bus>\d+)\s*[:=]\s*(?P<body>.*)$", re.I)


def parse_i2c(output: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    bus = "unknown"
    for line in output.splitlines():
        text = line.strip()
        bus_match = BUS_RE.match(text)
        if bus_match and not re.match(r"^[0-9a-fA-F]{2}:", text):
            bus, text = bus_match.group("bus"), bus_match.group("body")
        row_match = re.match(r"^(?P<base>[0-9a-fA-F]{2}):\s+(?P<cells>.*)$", text)
        if not row_match:
            continue
        for offset, cell in enumerate(row_match.group("cells").split()[:16]):
            if cell in {"--", "UU"} or not re.fullmatch(r"[0-9a-fA-F]{2}", cell):
                if cell != "UU":
                    continue
            address = int(row_match.group("base"), 16) + offset
            items.append(
                {
                    "source": "bmc",
                    "kind": "i2c_device",
                    "key": f"{bus}:0x{address:02x}",
                    "bus": bus,
                    "address": f"0x{address:02x}",
                    "claimed": cell == "UU",
                    "health": "ok",
                }
            )
    return items


def collect(ctx: ServiceContext, target_id: str):
    target = ctx.target(target_id)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or not capability.i2c:
        raise ServiceError("not_configured", 424)
    command = ctx.command(target, "i2c_health")
    result = ctx.provider.run_bmc_command(target, command)
    if not result.ok:
        raise ServiceError(result.error or "i2c_collection_failed", 502)
    items = parse_i2c(result.stdout)
    snapshot = new_snapshot(
        target.id,
        SnapshotKind.I2C,
        raw={"stdout": result.stdout, "stderr": result.stderr},
        normalized={
            "items": items,
            "count": len(items),
            "health": "ok" if items else "unknown",
        },
    )
    return ctx.store.save_snapshot(snapshot)
