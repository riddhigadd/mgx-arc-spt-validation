"""Shared service context, target lookup, and trusted-command handling."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from backend.config import MgxArcConfig, is_placeholder
from backend.jobs import JobManager
from backend.models import Snapshot, SnapshotKind, Target, to_plain, utc_now_iso
from backend.providers.base import TargetProvider
from backend.store import SnapshotStore


class ServiceError(RuntimeError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class ServiceContext:
    config: MgxArcConfig
    provider: TargetProvider
    store: SnapshotStore
    jobs: JobManager

    def target(self, target_id: str) -> Target:
        try:
            return self.config.target(target_id)
        except KeyError as exc:
            raise ServiceError("target_not_found", 404) from exc

    def target_payload(self, target: Target) -> dict[str, Any]:
        payload = to_plain(target)
        payload["configured"] = {
            "bmc": target.bmc_configured,
            "host": target.host_configured,
            "recovery": bool(self.command(target, "recovery", required=False)),
        }
        payload["capabilities"] = to_plain(
            self.config.capability_profiles.get(target.capability_profile)
        )
        return payload

    def command(self, target: Target, key: str, *, required: bool = True) -> str | None:
        """Return a command exclusively from the target's trusted YAML profile."""
        profile = self.config.profiles.get(target.expected_profile, {})
        commands = profile.get("commands", {}) if isinstance(profile, dict) else {}
        value: Any = commands
        for part in key.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        command = str(value or "").strip()
        if not command or is_placeholder(command):
            if required:
                raise ServiceError("not_configured", 424)
            return None
        return command


def new_snapshot(
    target_id: str,
    kind: SnapshotKind,
    *,
    raw: dict[str, Any],
    normalized: dict[str, Any],
    meta: dict[str, Any] | None = None,
) -> Snapshot:
    return Snapshot(
        id=str(uuid.uuid4()),
        target_id=target_id,
        kind=kind,
        created_at=utc_now_iso(),
        raw=raw,
        normalized=normalized,
        meta=dict(meta or {}),
    )


def compare_rows(left: Snapshot, right: Snapshot, key: str = "key") -> dict[str, Any]:
    left_rows = left.normalized.get("items", [])
    right_rows = right.normalized.get("items", [])
    before = {str(row.get(key)): row for row in left_rows if isinstance(row, dict)}
    after = {str(row.get(key)): row for row in right_rows if isinstance(row, dict)}
    added = [after[k] for k in sorted(after.keys() - before.keys())]
    removed = [before[k] for k in sorted(before.keys() - after.keys())]
    changed = [
        {"key": k, "before": before[k], "after": after[k]}
        for k in sorted(before.keys() & after.keys())
        if before[k] != after[k]
    ]
    return {
        "from_snapshot_id": left.id,
        "to_snapshot_id": right.id,
        "added": added,
        "removed": removed,
        "changed": changed,
        "summary": {"added": len(added), "removed": len(removed), "changed": len(changed)},
    }


def require_snapshot(ctx: ServiceContext, snapshot_id: str) -> Snapshot:
    snapshot = ctx.store.get_snapshot(snapshot_id)
    if snapshot is None:
        raise ServiceError("snapshot_not_found", 404)
    return snapshot
