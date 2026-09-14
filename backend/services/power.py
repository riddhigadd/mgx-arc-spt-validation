"""Redfish power state and safe reset actions."""

from __future__ import annotations

import uuid
from typing import Any

from backend.models import ActionKind, ActionRecord, to_plain
from backend.services.connection import ServiceContext, ServiceError

SYSTEM_PATH = "/Systems/system"
RESET_PATH = "/Systems/system/Actions/ComputerSystem.Reset"
RESET_TYPES = {
    "on": "On",
    "off": "ForceOff",
    "graceful_shutdown": "GracefulShutdown",
    "restart": "ForceRestart",
    "graceful_restart": "GracefulRestart",
    "power_cycle": "PowerCycle",
}


def status(ctx: ServiceContext, target_id: str) -> dict[str, Any]:
    target = ctx.target(target_id)
    result = ctx.provider.redfish_get(target, SYSTEM_PATH)
    if not result.ok:
        raise ServiceError(result.error or "power_status_failed", result.status_code or 502)
    data = result.data if isinstance(result.data, dict) else {}
    return {
        "target_id": target.id,
        "power_state": data.get("PowerState", "Unknown"),
        "health": (data.get("Status") or {}).get("Health", "Unknown"),
        "raw": data,
    }


def action(ctx: ServiceContext, target_id: str, requested: str, actor: str = "api") -> dict[str, Any]:
    target = ctx.target(target_id)
    action_name = requested.strip().lower()
    reset_type = RESET_TYPES.get(action_name)
    if reset_type is None:
        raise ServiceError("unsupported_power_action", 400)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or not capability.redfish_power:
        raise ServiceError("not_configured", 424)
    if action_name.startswith("graceful") and not capability.graceful_shutdown:
        raise ServiceError("action_not_supported", 409)
    result = ctx.provider.redfish_post(target, RESET_PATH, {"ResetType": reset_type})
    record = ActionRecord(
        id=str(uuid.uuid4()),
        action=ActionKind.POWER,
        target_id=target.id,
        actor=actor,
        status="complete" if result.ok else "failed",
        detail={"requested": action_name, "reset_type": reset_type, "error": result.error},
    )
    ctx.store.save_activity(record)
    if not result.ok:
        raise ServiceError(result.error or "power_action_failed", result.status_code or 502)
    return {"ok": True, "action": action_name, "record": to_plain(record)}


def history(ctx: ServiceContext, target_id: str, limit: int = 100) -> list[dict[str, Any]]:
    ctx.target(target_id)
    return [
        to_plain(item)
        for item in ctx.store.list_activity(target_id=target_id, limit=limit)
        if str(item.action) == str(ActionKind.POWER)
    ]
