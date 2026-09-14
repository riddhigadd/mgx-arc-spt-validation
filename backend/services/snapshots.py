"""Snapshot collection, retrieval, and comparison orchestration."""

from __future__ import annotations

from typing import Any, Callable

from backend.models import SnapshotKind, to_plain
from backend.services.connection import ServiceContext, ServiceError, compare_rows, new_snapshot


def _collector(kind: str) -> Callable[..., Any]:
    if kind == "usb":
        from backend.services.usb import collect
    elif kind == "mctp":
        from backend.services.mctp import collect
    elif kind == "i2c":
        from backend.services.i2c import collect
    elif kind == "inventory":
        from backend.services.inventory import collect
    elif kind == "firmware":
        from backend.services.firmware import collect
    else:
        raise ServiceError("unsupported_snapshot_kind", 400)
    return collect


def create(ctx: ServiceContext, target_id: str, kind: str, *, source: str = "bmc") -> dict[str, Any]:
    ctx.target(target_id)
    normalized_kind = kind.strip().lower()
    if normalized_kind != "aggregate":
        collector = _collector(normalized_kind)
        snapshot = (
            collector(ctx, target_id, source=source)
            if normalized_kind == "usb"
            else collector(ctx, target_id)
        )
        return {"snapshot": to_plain(snapshot)}

    def run(job_ctx):
        results: dict[str, Any] = {}
        snapshot_ids: dict[str, str] = {}
        kinds = ("usb", "mctp", "i2c", "inventory", "firmware")
        for index, item_kind in enumerate(kinds, start=1):
            job_ctx.update(percent=index * 15, message=f"Collecting {item_kind}")
            try:
                snapshot = _collector(item_kind)(ctx, target_id)
                snapshot_ids[item_kind] = snapshot.id
                results[item_kind] = snapshot.normalized
                job_ctx.log(f"{item_kind}: {snapshot.id}", "info")
            except ServiceError as exc:
                results[item_kind] = {"error": str(exc)}
                job_ctx.log(f"{item_kind}: {exc}", "err")
        aggregate = new_snapshot(
            target_id,
            SnapshotKind.AGGREGATE,
            raw={"snapshot_ids": snapshot_ids},
            normalized={"collections": results},
        )
        ctx.store.save_snapshot(aggregate)
        return {"snapshot_id": aggregate.id, "children": snapshot_ids}

    job = ctx.jobs.submit(
        "aggregate_snapshot",
        run,
        target_id=target_id,
        timeout=600,
        message="Aggregate snapshot queued",
    )
    return {"job": to_plain(job)}


def list_snapshots(
    ctx: ServiceContext,
    *,
    target_id: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if target_id:
        ctx.target(target_id)
    try:
        snapshot_kind = SnapshotKind(kind) if kind else None
    except ValueError as exc:
        raise ServiceError("invalid_snapshot_kind", 400) from exc
    return [
        to_plain(item)
        for item in ctx.store.list_snapshots(target_id=target_id, kind=snapshot_kind, limit=limit)
    ]


def get(ctx: ServiceContext, snapshot_id: str) -> dict[str, Any]:
    snapshot = ctx.store.get_snapshot(snapshot_id)
    if snapshot is None:
        raise ServiceError("snapshot_not_found", 404)
    return to_plain(snapshot)


def compare(ctx: ServiceContext, first_id: str, second_id: str) -> dict[str, Any]:
    first, second = ctx.store.get_snapshot(first_id), ctx.store.get_snapshot(second_id)
    if not first or not second:
        raise ServiceError("snapshot_not_found", 404)
    if first.kind != second.kind:
        raise ServiceError("snapshot_kind_mismatch", 400)
    if first.target_id != second.target_id:
        raise ServiceError("snapshot_target_mismatch", 400)
    if first.kind == SnapshotKind.AGGREGATE:
        return {
            "from_snapshot_id": first.id,
            "to_snapshot_id": second.id,
            "before": first.normalized,
            "after": second.normalized,
            "changed": first.normalized != second.normalized,
        }
    return compare_rows(first, second)
