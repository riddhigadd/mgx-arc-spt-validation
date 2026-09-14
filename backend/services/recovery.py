"""Configured MCU recovery background jobs."""

from __future__ import annotations

from typing import Any

from backend.models import to_plain
from backend.services.connection import ServiceContext, ServiceError


def start(ctx: ServiceContext, target_id: str) -> dict[str, Any]:
    target = ctx.target(target_id)
    capability = ctx.config.capability_profiles.get(target.capability_profile)
    if capability is None or not capability.mcu_recovery:
        raise ServiceError("not_configured", 424)
    command = ctx.command(target, "recovery")

    def run(job_ctx):
        job_ctx.log(f"Starting configured recovery for {target.id}", "info")
        job_ctx.log(command, "cmd")
        job_ctx.update(percent=10, message="Running recovery")
        result = ctx.provider.run_bmc_command(target, command, timeout=300)
        for line in result.stdout.splitlines():
            job_ctx.log(line, "out")
        for line in result.stderr.splitlines():
            job_ctx.log(line, "err")
        if not result.ok:
            raise RuntimeError(result.error or "Recovery command failed.")
        job_ctx.update(percent=95, message="Finalizing recovery")
        return {"ok": True, "exit_code": result.exit_code}

    job = ctx.jobs.submit(
        "recovery",
        run,
        target_id=target.id,
        timeout=360,
        message="Recovery queued",
    )
    return to_plain(job)


def get_job(ctx: ServiceContext, job_id: str) -> dict[str, Any]:
    job = ctx.jobs.get(job_id)
    if job is None or job.kind != "recovery":
        raise ServiceError("job_not_found", 404)
    return to_plain(job)


def logs(ctx: ServiceContext, job_id: str) -> list[dict[str, Any]]:
    get_job(ctx, job_id)
    return [to_plain(step) for step in ctx.jobs.logs(job_id)]
