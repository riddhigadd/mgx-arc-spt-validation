"""Redfish hardware inventory collection."""

from __future__ import annotations

from typing import Any

from backend.models import SnapshotKind
from backend.services.connection import ServiceContext, ServiceError, compare_rows, new_snapshot

INVENTORY_PATHS = (
    "/Systems/system",
    "/Chassis/chassis",
    "/Managers/bmc",
)


def _component(path: str, data: dict[str, Any]) -> dict[str, Any]:
    status = data.get("Status") if isinstance(data.get("Status"), dict) else {}
    component_id = str(data.get("Id") or path.rsplit("/", 1)[-1])
    return {
        "key": component_id,
        "component_id": component_id,
        "name": str(data.get("Name") or data.get("Model") or component_id),
        "manufacturer": str(data.get("Manufacturer") or ""),
        "model": str(data.get("Model") or ""),
        "serial_number": str(data.get("SerialNumber") or ""),
        "part_number": str(data.get("PartNumber") or ""),
        "health": str(status.get("Health") or status.get("State") or ""),
        "source": "redfish",
        "path": path,
    }


def collect(ctx: ServiceContext, target_id: str):
    target = ctx.target(target_id)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or not capability.fru_redfish:
        raise ServiceError("not_configured", 424)
    raw: dict[str, Any] = {}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for path in INVENTORY_PATHS:
        result = ctx.provider.redfish_get(target, path)
        if result.ok and isinstance(result.data, dict):
            raw[path] = result.data
            items.append(_component(path, result.data))
        else:
            errors.append({"path": path, "error": result.error, "status": result.status_code})
    if not items:
        first = errors[0] if errors else {}
        raise ServiceError(str(first.get("error") or "inventory_collection_failed"), 502)
    snapshot = new_snapshot(
        target.id,
        SnapshotKind.INVENTORY,
        raw=raw,
        normalized={"items": items, "count": len(items), "errors": errors},
    )
    return ctx.store.save_snapshot(snapshot)


def compare(ctx: ServiceContext, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = ctx.store.get_snapshot(first_id), ctx.store.get_snapshot(second_id)
    if not first or not second:
        raise ServiceError("snapshot_not_found", 404)
    if first.kind != SnapshotKind.INVENTORY or second.kind != SnapshotKind.INVENTORY:
        raise ServiceError("snapshot_kind_mismatch", 400)
    return compare_rows(first, second)
