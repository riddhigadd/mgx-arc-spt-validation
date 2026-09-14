"""Provider protocol for BMC/host transport."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.models import (
    CommandResult,
    ConnectionState,
    ConnectionStatus,
    RedfishResult,
    Target,
)


@runtime_checkable
class JumpRoute(Protocol):
    """Optional hop used when a target declares a configured jump host."""

    def open_ssh(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 22,
        timeout: float | None = None,
        label: str = "BMC",
    ) -> Any:
        """Return a connected Paramiko SSH client to ``host`` via this route."""

    def open_tcp(self, host: str, port: int, timeout: float | None = None) -> Any:
        """Return a connected socket (or channel) to ``host:port`` via this route."""

    def close(self) -> None:
        """Release jump-host resources."""


@runtime_checkable
class TargetProvider(Protocol):
    """Redfish + SSH transport used by future ``/api/arc`` services."""

    def redfish_get(self, target: Target, path: str) -> RedfishResult:
        ...

    def redfish_post(self, target: Target, path: str, payload: dict[str, Any] | None = None) -> RedfishResult:
        ...

    def redfish_delete(self, target: Target, path: str) -> RedfishResult:
        ...

    def redfish_request(
        self,
        target: Target,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> RedfishResult:
        ...

    def run_bmc_command(
        self,
        target: Target,
        command: str,
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        ...

    def run_host_command(
        self,
        target: Target,
        command: str,
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        ...

    def check_bmc_reachable(self, target: Target) -> ConnectionStatus:
        ...

    def check_host_reachable(self, target: Target) -> ConnectionStatus:
        ...

    def check_reachability(self, target: Target) -> ConnectionState:
        ...
