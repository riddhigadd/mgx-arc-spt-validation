"""Dataclasses and enums for MGX ARC health-console records."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_plain(value: Any) -> Any:
    """JSON-friendly conversion for enums, dataclasses, and nested containers."""
    if isinstance(value, StrEnum):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {k: to_plain(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {
            str(k): ("***" if "password" in str(k).lower() else to_plain(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    return value


class ConnectionStatus(StrEnum):
    CONNECTED = "connected"
    REACHABLE = "reachable"
    AUTH_FAILED = "auth_failed"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class IssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class IssueStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class SnapshotKind(StrEnum):
    USB = "usb"
    MCTP = "mctp"
    I2C = "i2c"
    INVENTORY = "inventory"
    FIRMWARE = "firmware"
    POWER = "power"
    AGGREGATE = "aggregate"


class CollectSource(StrEnum):
    BMC = "bmc"
    HOST = "host"
    REDFISH = "redfish"
    AGGREGATE = "aggregate"


class ActionKind(StrEnum):
    CONNECT = "connect"
    POWER = "power"
    COLLECT = "collect"
    COMPARE = "compare"
    RECOVERY = "recovery"
    SNAPSHOT = "snapshot"
    ISSUE_UPDATE = "issue_update"


@dataclass(frozen=True)
class CredentialSpec:
    id: str
    username_env: str
    password_env: str


@dataclass
class ResolvedCredential:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"ResolvedCredential(username={self.username!r}, password='***')"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True)
class JumpHostSpec:
    id: str
    host: str
    credential_ref: str
    port: int = 22
    placeholders: tuple[str, ...] = ()

    @property
    def is_configured(self) -> bool:
        return not self.placeholders


@dataclass(frozen=True)
class CapabilityProfile:
    id: str
    redfish_power: bool = True
    graceful_shutdown: bool = True
    ac_cycle: bool = False
    usb_sources: tuple[str, ...] = ("bmc", "host")
    mctp: bool = True
    i2c: bool = True
    fru_redfish: bool = True
    fru_ipmi: bool = False
    mcu_recovery: bool = False


@dataclass(frozen=True)
class Target:
    id: str
    chassis_id: str
    node_id: str
    label: str
    bmc_host: str
    os_host: str
    credential_ref: str
    host_credential_ref: str | None = None
    jump_host_ref: str | None = None
    tags: tuple[str, ...] = ()
    revision: str = ""
    expected_profile: str = ""
    capability_profile: str = ""
    placeholders: tuple[str, ...] = ()

    @property
    def bmc_configured(self) -> bool:
        return bool(self.bmc_host) and "TODO_MGX_ARC_" not in self.bmc_host

    @property
    def host_configured(self) -> bool:
        return bool(self.os_host) and "TODO_MGX_ARC_" not in self.os_host


@dataclass
class ConnectionState:
    target_id: str
    bmc: ConnectionStatus = ConnectionStatus.UNKNOWN
    host: ConnectionStatus = ConnectionStatus.UNKNOWN
    bmc_detail: str = ""
    host_detail: str = ""
    checked_at: str = field(default_factory=utc_now_iso)


@dataclass
class NormalizedDevice:
    """Normalized peripheral/device row (USB, MCTP endpoint, I2C address, …)."""

    source: CollectSource
    kind: str
    key: str
    label: str = ""
    vid: str | None = None
    pid: str | None = None
    address: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedComponent:
    """Normalized inventory/firmware component."""

    component_id: str
    name: str
    version: str = ""
    health: str = ""
    source: CollectSource = CollectSource.REDFISH
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Snapshot:
    id: str
    target_id: str
    kind: SnapshotKind
    created_at: str
    raw: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Issue:
    id: str
    target_id: str
    code: str
    title: str
    severity: IssueSeverity = IssueSeverity.WARNING
    status: IssueStatus = IssueStatus.OPEN
    detail: str = ""
    snapshot_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionRecord:
    id: str
    action: ActionKind
    target_id: str | None = None
    actor: str = "system"
    status: str = "recorded"
    created_at: str = field(default_factory=utc_now_iso)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobLogStep:
    job_id: str
    seq: int
    line: str
    style: str = "out"
    created_at: str = field(default_factory=utc_now_iso)
    id: int | None = None


@dataclass
class Job:
    id: str
    kind: str
    status: JobStatus = JobStatus.QUEUED
    target_id: str | None = None
    percent: int = 0
    message: str = ""
    error: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CommandResult:
    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    command: str
    error: str | None = None
    timed_out: bool = False


@dataclass
class RedfishResult:
    ok: bool
    status_code: int
    data: Any = None
    error: str | None = None
    url: str = ""
