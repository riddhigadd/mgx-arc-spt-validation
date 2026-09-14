"""Paramiko/requests MGX ARC provider.

Transport behavior matches the existing ``app.py`` helpers (timeouts, Basic
Redfish auth, Paramiko password SSH, TLS verify disabled for lab BMCs) but is
credential- and jump-host-aware for future ``/api/arc`` routes. Current
``/api/bmc/*`` handlers still use the helpers in ``app.py`` directly.
"""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import requests
import urllib3
from requests.auth import HTTPBasicAuth

from backend.config import ConfigError, MgxArcConfig, is_placeholder, resolve_credential
from backend.models import (
    CommandResult,
    ConnectionState,
    ConnectionStatus,
    RedfishResult,
    ResolvedCredential,
    Target,
    utc_now_iso,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 15
SSH_PORT = 22
SSH_TIMEOUT = 8
SSH_CMD_TIMEOUT = 12
REDFISH_POOL_SIZE = 8


class ProviderError(RuntimeError):
    """Transport-level failure that is not a BMC command error."""


class DirectRoute:
    """No jump host: connect to the destination directly."""

    def open_ssh(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = SSH_PORT,
        timeout: float | None = None,
        label: str = "BMC",
    ):
        return _paramiko_connect(
            host,
            username,
            password,
            port=port,
            timeout=timeout,
            label=label,
            sock=None,
        )

    def open_tcp(self, host: str, port: int, timeout: float | None = None) -> socket.socket:
        sock = socket.create_connection((host, port), timeout=timeout or CONNECT_TIMEOUT)
        return sock

    def close(self) -> None:
        return None


class ParamikoJumpRoute:
    """SSH jump-host route using Paramiko ``direct-tcpip`` channels."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = SSH_PORT,
        timeout: float | None = None,
    ) -> None:
        self._jump_client = _paramiko_connect(
            host,
            username,
            password,
            port=port,
            timeout=timeout,
            label="jump host",
            sock=None,
        )
        self._lock = threading.Lock()

    def open_ssh(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = SSH_PORT,
        timeout: float | None = None,
        label: str = "BMC",
    ):
        channel = self.open_tcp(host, port, timeout=timeout)
        return _paramiko_connect(
            host,
            username,
            password,
            port=port,
            timeout=timeout,
            label=label,
            sock=channel,
        )

    def open_tcp(self, host: str, port: int, timeout: float | None = None):
        transport = self._jump_client.get_transport()
        if transport is None or not transport.is_active():
            raise ProviderError("Jump-host SSH transport is not active.")
        wait = timeout or SSH_TIMEOUT
        transport.set_keepalive(30)
        return transport.open_channel(
            "direct-tcpip",
            (host, port),
            ("127.0.0.1", 0),
            timeout=wait,
        )

    def close(self) -> None:
        with self._lock:
            try:
                self._jump_client.close()
            except Exception:
                pass


class ArcProvider:
    """Target-scoped SSH/Redfish client for MGX ARC nodes."""

    def __init__(self, config: MgxArcConfig) -> None:
        self.config = config
        self._local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=4,
                pool_maxsize=REDFISH_POOL_SIZE,
                max_retries=0,
            )
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def redfish_get(self, target: Target, path: str) -> RedfishResult:
        return self.redfish_request(target, "GET", path)

    def redfish_post(
        self, target: Target, path: str, payload: dict[str, Any] | None = None
    ) -> RedfishResult:
        return self.redfish_request(target, "POST", path, payload)

    def redfish_delete(self, target: Target, path: str) -> RedfishResult:
        return self.redfish_request(target, "DELETE", path)

    def redfish_request(
        self,
        target: Target,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> RedfishResult:
        blocked = self._not_configured(target, need_bmc=True)
        if blocked is not None:
            return blocked
        url = ""
        try:
            cred = self._bmc_credential(target)
            with self._route(target) as route:
                with self._redfish_endpoint(target, path, route) as url:
                    resp = self._session().request(
                        method.upper(),
                        url,
                        auth=HTTPBasicAuth(cred.username, cred.password),
                        json=payload if method.upper() in {"POST", "PATCH", "PUT"} else None,
                        verify=False,
                        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    )
        except requests.exceptions.ConnectTimeout:
            return RedfishResult(False, 504, error="Connection timed out reaching the BMC.")
        except requests.exceptions.ConnectionError:
            return RedfishResult(False, 502, error="Could not reach the BMC on the network.")
        except requests.exceptions.RequestException as exc:
            return RedfishResult(False, 502, error=f"Request failed: {exc}")
        except (ProviderError, ConfigError, OSError) as exc:
            return RedfishResult(False, 502, error=str(exc))

        if resp.status_code == 401:
            return RedfishResult(False, 401, error="BMC rejected the credentials.", url=url)
        if resp.status_code >= 400:
            detail = _redfish_error_body(resp)
            return RedfishResult(
                False,
                resp.status_code,
                error=f"BMC returned HTTP {resp.status_code}. {detail}".strip(),
                url=url,
            )
        if method.upper() == "DELETE":
            return RedfishResult(True, resp.status_code, data=resp.text[:200], url=url)
        try:
            return RedfishResult(True, resp.status_code, data=resp.json(), url=url)
        except ValueError:
            if not resp.content:
                return RedfishResult(True, resp.status_code, data={"status": resp.status_code}, url=url)
            return RedfishResult(False, 502, error="BMC returned a non-JSON response.", url=url)

    def run_bmc_command(
        self,
        target: Target,
        command: str,
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        blocked = self._command_not_configured(target, command, need_bmc=True)
        if blocked is not None:
            return blocked
        try:
            credential = self._bmc_credential(target)
        except ConfigError:
            return CommandResult(False, 1, "", "", command, error="not_configured")
        return self._run_ssh(
            target,
            host=target.bmc_host,
            credential=credential,
            command=command,
            timeout=timeout,
            label="BMC",
        )

    def run_host_command(
        self,
        target: Target,
        command: str,
        *,
        timeout: float | None = None,
    ) -> CommandResult:
        blocked = self._command_not_configured(target, command, need_host=True)
        if blocked is not None:
            return blocked
        try:
            credential = self._host_credential(target)
        except ConfigError:
            return CommandResult(False, 1, "", "", command, error="not_configured")
        return self._run_ssh(
            target,
            host=target.os_host,
            credential=credential,
            command=command,
            timeout=timeout,
            label="host",
        )

    def check_bmc_reachable(self, target: Target) -> ConnectionStatus:
        return self.check_reachability(target).bmc

    def check_host_reachable(self, target: Target) -> ConnectionStatus:
        return self.check_reachability(target).host

    def check_reachability(self, target: Target) -> ConnectionState:
        state = ConnectionState(target_id=target.id, checked_at=utc_now_iso())
        if not target.bmc_configured or self._jump_blocked(target):
            state.bmc = ConnectionStatus.NOT_CONFIGURED
            state.bmc_detail = "BMC host or jump host is still a TODO_MGX_ARC_* placeholder."
        else:
            state.bmc, state.bmc_detail = self._probe_https_or_ssh(target, target.bmc_host, "BMC")
        if not target.host_configured or self._jump_blocked(target):
            state.host = ConnectionStatus.NOT_CONFIGURED
            state.host_detail = "OS host or jump host is still a TODO_MGX_ARC_* placeholder."
        else:
            state.host, state.host_detail = self._probe_ssh_only(target, target.os_host, "host")
        return state

    def _run_ssh(
        self,
        target: Target,
        *,
        host: str,
        credential: ResolvedCredential,
        command: str,
        timeout: float | None,
        label: str,
    ) -> CommandResult:
        command = (command or "").strip()
        if not command:
            return CommandResult(False, 1, "", "", command, error="Command is required.")
        client = None
        try:
            with self._route(target) as route:
                client = route.open_ssh(
                    host,
                    credential.username,
                    credential.password,
                    timeout=SSH_TIMEOUT,
                    label=label,
                )
                code, out, err = _ssh_exec(client, command, timeout=timeout or SSH_CMD_TIMEOUT)
            timed_out = "timed out" in (err or "").lower()
            return CommandResult(
                ok=code == 0,
                exit_code=code,
                stdout=out,
                stderr=err,
                command=command,
                error=None if code == 0 else (err or f"Command failed (exit {code})."),
                timed_out=timed_out,
            )
        except Exception as exc:  # noqa: BLE001 - surface transport failures cleanly
            status = _status_from_exc(exc)
            return CommandResult(
                False,
                1,
                "",
                str(exc),
                command,
                error=str(exc),
                timed_out=status == ConnectionStatus.TIMED_OUT,
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _probe_https_or_ssh(self, target: Target, host: str, label: str) -> tuple[ConnectionStatus, str]:
        try:
            with self._route(target) as route:
                sock = route.open_tcp(host, 443, timeout=CONNECT_TIMEOUT)
                try:
                    sock.close()
                except Exception:
                    pass
            return ConnectionStatus.REACHABLE, f"{label} HTTPS port is reachable."
        except TimeoutError as exc:
            return ConnectionStatus.TIMED_OUT, str(exc)
        except Exception:
            return self._probe_ssh_only(target, host, label)

    def _probe_ssh_only(self, target: Target, host: str, label: str) -> tuple[ConnectionStatus, str]:
        try:
            cred = self._bmc_credential(target) if label == "BMC" else self._host_credential(target)
        except ConfigError as exc:
            return ConnectionStatus.NOT_CONFIGURED, str(exc)
        client = None
        try:
            with self._route(target) as route:
                client = route.open_ssh(
                    host,
                    cred.username,
                    cred.password,
                    timeout=SSH_TIMEOUT,
                    label=label,
                )
            return ConnectionStatus.CONNECTED, f"{label} accepted SSH credentials."
        except Exception as exc:  # noqa: BLE001
            return _status_from_exc(exc), str(exc)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @contextmanager
    def _route(self, target: Target) -> Iterator[DirectRoute | ParamikoJumpRoute]:
        if not target.jump_host_ref:
            yield DirectRoute()
            return
        if self._jump_blocked(target):
            raise ProviderError("Jump host is not configured (TODO_MGX_ARC_* placeholder).")
        spec = self.config.jump_host(target.jump_host_ref)
        cred = resolve_credential(self.config.credentials[spec.credential_ref])
        route = ParamikoJumpRoute(
            spec.host,
            cred.username,
            cred.password,
            port=spec.port,
            timeout=SSH_TIMEOUT,
        )
        try:
            yield route
        finally:
            route.close()

    @contextmanager
    def _redfish_endpoint(
        self,
        target: Target,
        path: str,
        route: DirectRoute | ParamikoJumpRoute,
    ) -> Iterator[str]:
        rel = path if path.startswith("/") else f"/{path}"
        if rel.startswith("/redfish/v1"):
            rel = rel[len("/redfish/v1") :] or "/"
        if isinstance(route, DirectRoute):
            yield f"https://{target.bmc_host}/redfish/v1{rel}"
            return
        tunnel = _LocalJumpForward(route, target.bmc_host, 443)
        try:
            yield f"https://127.0.0.1:{tunnel.port}/redfish/v1{rel}"
        finally:
            tunnel.close()

    def _bmc_credential(self, target: Target) -> ResolvedCredential:
        return resolve_credential(self.config.credentials[target.credential_ref])

    def _host_credential(self, target: Target) -> ResolvedCredential:
        ref = target.host_credential_ref or target.credential_ref
        return resolve_credential(self.config.credentials[ref])

    def _jump_blocked(self, target: Target) -> bool:
        if not target.jump_host_ref:
            return False
        spec = self.config.jump_hosts.get(target.jump_host_ref)
        if spec is None:
            return True
        return not spec.is_configured or is_placeholder(spec.host) or is_placeholder(spec.credential_ref)

    def _not_configured(self, target: Target, *, need_bmc: bool = False) -> RedfishResult | None:
        if need_bmc and (not target.bmc_configured or self._jump_blocked(target)):
            return RedfishResult(
                False,
                424,
                error="not_configured",
            )
        return None

    def _command_not_configured(
        self,
        target: Target,
        command: str,
        *,
        need_bmc: bool = False,
        need_host: bool = False,
    ) -> CommandResult | None:
        if need_bmc and (not target.bmc_configured or self._jump_blocked(target)):
            return CommandResult(False, 1, "", "", command, error="not_configured")
        if need_host and (not target.host_configured or self._jump_blocked(target)):
            return CommandResult(False, 1, "", "", command, error="not_configured")
        return None


class _LocalJumpForward:
    """Forward 127.0.0.1:ephemeral -> dest_host:dest_port through a jump route."""

    def __init__(self, route: ParamikoJumpRoute, dest_host: str, dest_port: int) -> None:
        self._route = route
        self._dest_host = dest_host
        self._dest_port = dest_port
        self._stop = threading.Event()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(8)
        self._server.settimeout(0.3)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, name="mgx-arc-jump-fwd", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            worker = threading.Thread(target=self._pipe, args=(client,), daemon=True)
            worker.start()

    def _pipe(self, client: socket.socket) -> None:
        try:
            channel = self._route.open_tcp(self._dest_host, self._dest_port, timeout=CONNECT_TIMEOUT)
        except Exception:
            try:
                client.close()
            except OSError:
                pass
            return
        threads = [
            threading.Thread(target=_copy_sock_to_chan, args=(client, channel), daemon=True),
            threading.Thread(target=_copy_chan_to_sock, args=(channel, client), daemon=True),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for closer in (client, channel):
            try:
                closer.close()
            except Exception:
                pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except OSError:
            pass


def _copy_sock_to_chan(client: socket.socket, channel) -> None:
    try:
        while True:
            data = client.recv(16384)
            if not data:
                break
            channel.sendall(data)
    except Exception:
        pass


def _copy_chan_to_sock(channel, client: socket.socket) -> None:
    try:
        while True:
            data = channel.recv(16384)
            if not data:
                break
            client.sendall(data)
    except Exception:
        pass


def _paramiko_connect(
    host: str,
    username: str,
    password: str,
    *,
    port: int,
    timeout: float | None,
    label: str,
    sock,
):
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - declared in requirements.txt
        raise ProviderError(
            "paramiko is not installed. Run 'pip install -r requirements.txt'."
        ) from exc

    wait = timeout or SSH_TIMEOUT
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host,
            port=port,
            username=username,
            password=password,
            timeout=wait,
            banner_timeout=wait,
            auth_timeout=wait,
            look_for_keys=False,
            allow_agent=False,
            sock=sock,
        )
    except paramiko.AuthenticationException as exc:
        raise ProviderError(f"{label} rejected the SSH credentials.") from exc
    except paramiko.SSHException as exc:
        raise ProviderError(f"SSH error reaching the {label}: {exc}") from exc
    except OSError as exc:
        raise ProviderError(f"Could not reach the {label} over SSH: {exc}") from exc
    return client


def _ssh_exec(client, command: str, timeout: float) -> tuple[int, str, str]:
    try:
        _stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", "replace").strip()
        err = stderr.read().decode("utf-8", "replace").strip()
        code = stdout.channel.recv_exit_status()
        return code, out, err
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _redfish_error_body(resp: requests.Response) -> str:
    try:
        body = resp.json()
        return str(body.get("error", {}).get("message", "") or "")
    except ValueError:
        return (resp.text or "")[:200]


def _status_from_exc(exc: BaseException) -> ConnectionStatus:
    text = str(exc).lower()
    name = exc.__class__.__name__.lower()
    if "auth" in text or "credential" in text or "authentication" in name:
        return ConnectionStatus.AUTH_FAILED
    if "timed out" in text or "timeout" in text or "timeout" in name:
        return ConnectionStatus.TIMED_OUT
    return ConnectionStatus.UNKNOWN
