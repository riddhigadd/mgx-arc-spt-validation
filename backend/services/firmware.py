"""Redfish firmware inventory collection."""

from __future__ import annotations

from typing import Any

from backend.models import SnapshotKind
from backend.services.connection import ServiceContext, ServiceError, compare_rows, new_snapshot

COLLECTION_PATH = "/UpdateService/FirmwareInventory"


def _normalize(data: dict[str, Any], path: str) -> dict[str, Any]:
    status = data.get("Status") if isinstance(data.get("Status"), dict) else {}
    component_id = str(data.get("Id") or path.rsplit("/", 1)[-1])
    return {
        "key": component_id,
        "component_id": component_id,
        "name": str(data.get("Name") or component_id),
        "version": str(data.get("Version") or data.get("SoftwareId") or ""),
        "health": str(status.get("Health") or status.get("State") or ""),
        "updateable": bool(data.get("Updateable", False)),
        "source": "redfish",
    }


def collect(ctx: ServiceContext, target_id: str):
    target = ctx.target(target_id)
    result = ctx.provider.redfish_get(target, COLLECTION_PATH)
    if not result.ok or not isinstance(result.data, dict):
        raise ServiceError(result.error or "firmware_collection_failed", result.status_code or 502)
    raw: dict[str, Any] = {COLLECTION_PATH: result.data}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    members = result.data.get("Members", [])
    if not isinstance(members, list):
        members = []
    for member in members:
        path = member.get("@odata.id") if isinstance(member, dict) else None
        if not isinstance(path, str):
            continue
        detail = ctx.provider.redfish_get(target, path)
        if detail.ok and isinstance(detail.data, dict):
            raw[path] = detail.data
            items.append(_normalize(detail.data, path))
        else:
            errors.append({"path": path, "error": detail.error, "status": detail.status_code})
    snapshot = new_snapshot(
        target.id,
        SnapshotKind.FIRMWARE,
        raw=raw,
        normalized={"items": items, "count": len(items), "errors": errors},
    )
    return ctx.store.save_snapshot(snapshot)


def compare(ctx: ServiceContext, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = ctx.store.get_snapshot(first_id), ctx.store.get_snapshot(second_id)
    if not first or not second:
        raise ServiceError("snapshot_not_found", 404)
    if first.kind != SnapshotKind.FIRMWARE or second.kind != SnapshotKind.FIRMWARE:
        raise ServiceError("snapshot_kind_mismatch", 400)
    return compare_rows(first, second)
