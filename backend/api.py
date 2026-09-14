"""Flask Blueprint for the configuration-driven MGX ARC API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import Blueprint, Flask, jsonify, request

from backend.config import MgxArcConfig, load_config
from backend.jobs import JobManager
from backend.models import ActionKind, ActionRecord, IssueStatus, to_plain
from backend.providers.arc import ArcProvider
from backend.providers.base import TargetProvider
from backend.services import firmware, inventory, mctp, power, recovery, snapshots, usb
from backend.services.connection import ServiceContext, ServiceError
from backend.services.i2c import collect as collect_i2c
from backend.store import SnapshotStore


@dataclass
class ArcApiState:
    config: MgxArcConfig | None
    provider: TargetProvider | None
    store: SnapshotStore
    jobs: JobManager
    config_error: str | None = None

    def context(self) -> ServiceContext:
        if self.config is None or self.provider is None:
            raise ServiceError("config_error", 503)
        return ServiceContext(self.config, self.provider, self.store, self.jobs)


def initialize_arc_api(
    app: Flask,
    *,
    config: MgxArcConfig | None = None,
    provider: TargetProvider | None = None,
    store: SnapshotStore | None = None,
    jobs: JobManager | None = None,
) -> ArcApiState:
    """Initialize dependencies and register ``/api/arc`` without blocking app startup."""
    config_error: str | None = None
    if config is None:
        try:
            config = load_config()
        except Exception as exc:  # configuration must not disable legacy routes
            config_error = str(exc)
    store = store or SnapshotStore()
    jobs = jobs or JobManager(store)
    if config is not None and provider is None:
        provider = ArcProvider(config)
    state = ArcApiState(config, provider, store, jobs, config_error)
    app.extensions["mgx_arc"] = state
    app.config["MGX_ARC_CONFIG_ERROR"] = config_error
    app.register_blueprint(_create_blueprint(state), url_prefix="/api/arc")
    return state


def _create_blueprint(state: ArcApiState) -> Blueprint:
    bp = Blueprint("mgx_arc_api", __name__)

    def ok(data: Any, status: int = 200):
        return jsonify(data), status

    def body() -> dict[str, Any]:
        value = request.get_json(silent=True)
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ServiceError("json_object_required", 400)
        return value

    def context() -> ServiceContext:
        return state.context()

    @bp.errorhandler(ServiceError)
    def service_error(exc: ServiceError):
        payload: dict[str, Any] = {"error": str(exc)}
        if str(exc) == "config_error":
            payload["config_error"] = state.config_error or "Configuration is unavailable."
        return jsonify(payload), exc.status

    @bp.errorhandler(ValueError)
    def value_error(exc: ValueError):
        return jsonify({"error": str(exc)}), 400

    @bp.get("")
    @bp.get("/")
    def api_status():
        return ok(
            {
                "ok": state.config_error is None,
                "config_error": state.config_error,
                "targets": len(state.config.systems) if state.config else 0,
            }
        )

    @bp.get("/targets")
    def targets_list():
        ctx = context()
        return ok({"items": [ctx.target_payload(item) for item in ctx.config.systems]})

    @bp.get("/targets/<target_id>")
    def target_get(target_id: str):
        ctx = context()
        return ok(ctx.target_payload(ctx.target(target_id)))

    @bp.get("/targets/<target_id>/status")
    def target_status(target_id: str):
        ctx = context()
        target = ctx.target(target_id)
        return ok(to_plain(ctx.provider.check_reachability(target)))

    @bp.post("/targets/<target_id>/test")
    def target_test(target_id: str):
        ctx = context()
        target = ctx.target(target_id)
        return ok({"target_id": target.id, "connection": to_plain(ctx.provider.check_reachability(target))})

    @bp.get("/targets/<target_id>/power")
    def power_status(target_id: str):
        return ok(power.status(context(), target_id))

    @bp.post("/targets/<target_id>/power")
    def power_action(target_id: str):
        data = body()
        return ok(power.action(context(), target_id, str(data.get("action") or ""), _actor()))

    @bp.get("/targets/<target_id>/power/history")
    def power_history(target_id: str):
        return ok({"items": power.history(context(), target_id, _limit())})

    @bp.post("/targets/<target_id>/usb")
    def usb_collect(target_id: str):
        snapshot = usb.collect(context(), target_id, str(body().get("source") or "bmc"))
        return ok({"snapshot": to_plain(snapshot)}, 201)

    @bp.post("/usb/compare")
    def usb_compare():
        data = body()
        return ok(usb.compare(context(), _required(data, "from"), _required(data, "to")))

    @bp.post("/targets/<target_id>/mctp")
    def mctp_collect(target_id: str):
        return ok({"snapshot": to_plain(mctp.collect(context(), target_id))}, 201)

    @bp.post("/mctp/compare")
    def mctp_compare():
        data = body()
        return ok(mctp.compare(context(), _required(data, "from"), _required(data, "to")))

    @bp.post("/targets/<target_id>/i2c/health")
    def i2c_health(target_id: str):
        return ok({"snapshot": to_plain(collect_i2c(context(), target_id))}, 201)

    @bp.post("/targets/<target_id>/inventory")
    def inventory_collect(target_id: str):
        return ok({"snapshot": to_plain(inventory.collect(context(), target_id))}, 201)

    @bp.post("/inventory/compare")
    def inventory_compare():
        data = body()
        return ok(inventory.compare(context(), _required(data, "from"), _required(data, "to")))

    @bp.post("/targets/<target_id>/firmware")
    def firmware_collect(target_id: str):
        return ok({"snapshot": to_plain(firmware.collect(context(), target_id))}, 201)

    @bp.post("/firmware/compare")
    def firmware_compare():
        data = body()
        return ok(firmware.compare(context(), _required(data, "from"), _required(data, "to")))

    @bp.post("/targets/<target_id>/recovery")
    def recovery_start(target_id: str):
        return ok({"job": recovery.start(context(), target_id)}, 202)

    @bp.get("/recovery/jobs/<job_id>")
    def recovery_job(job_id: str):
        return ok(recovery.get_job(context(), job_id))

    @bp.get("/recovery/jobs/<job_id>/log")
    def recovery_log(job_id: str):
        return ok({"items": recovery.logs(context(), job_id)})

    @bp.post("/snapshots")
    def snapshots_create():
        data = body()
        result = snapshots.create(
            context(),
            _required(data, "target_id"),
            str(data.get("kind") or "aggregate"),
            source=str(data.get("source") or "bmc"),
        )
        return ok(result, 202 if "job" in result else 201)

    @bp.get("/snapshots")
    def snapshots_list():
        return ok(
            {
                "items": snapshots.list_snapshots(
                    context(),
                    target_id=request.args.get("target_id"),
                    kind=request.args.get("kind"),
                    limit=_limit(),
                )
            }
        )

    @bp.get("/snapshots/<snapshot_id>")
    def snapshots_get(snapshot_id: str):
        return ok(snapshots.get(context(), snapshot_id))

    @bp.post("/snapshots/compare")
    def snapshots_compare():
        data = body()
        return ok(snapshots.compare(context(), _required(data, "from"), _required(data, "to")))

    @bp.get("/issues")
    def issues_list():
        ctx = context()
        status = request.args.get("status")
        if status:
            IssueStatus(status)
        items = ctx.store.list_issues(
            target_id=request.args.get("target_id"),
            status=status,
            limit=_limit(200),
        )
        return ok({"items": [to_plain(item) for item in items]})

    @bp.patch("/issues/<issue_id>")
    def issue_update(issue_id: str):
        ctx = context()
        data = body()
        allowed = {"status", "severity", "title", "detail"}
        fields = {key: value for key, value in data.items() if key in allowed}
        if not fields or set(data) - allowed:
            raise ServiceError("invalid_issue_update", 400)
        issue = ctx.store.update_issue(issue_id, **fields)
        if issue is None:
            raise ServiceError("issue_not_found", 404)
        ctx.store.save_activity(
            ActionRecord(
                id=__import__("uuid").uuid4().hex,
                action=ActionKind.ISSUE_UPDATE,
                target_id=issue.target_id,
                actor=_actor(),
                detail={"issue_id": issue.id, "fields": sorted(fields)},
            )
        )
        return ok(to_plain(issue))

    return bp


def _required(data: dict[str, Any], name: str) -> str:
    value = str(data.get(name) or "").strip()
    if not value:
        raise ServiceError(f"{name}_required", 400)
    return value


def _limit(default: int = 100) -> int:
    try:
        return max(1, min(1000, int(request.args.get("limit", default))))
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid_limit", 400) from exc


def _actor() -> str:
    return (request.headers.get("X-Actor") or "api")[:100]
