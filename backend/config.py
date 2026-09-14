"""YAML configuration loading, validation, and environment-backed credentials."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # surfaced as ConfigError so legacy Flask routes still start
    yaml = None

from backend.models import (
    CapabilityProfile,
    CredentialSpec,
    JumpHostSpec,
    ResolvedCredential,
    Target,
)

PLACEHOLDER_TOKEN = "TODO_MGX_ARC_"
SUPPORTED_VERSION = 1
SECRET_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "username",
        "user",
    }
)
ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """Invalid or unsafe MGX ARC configuration."""


@dataclass
class MgxArcConfig:
    version: int
    credentials: dict[str, CredentialSpec]
    jump_hosts: dict[str, JumpHostSpec]
    capability_profiles: dict[str, CapabilityProfile]
    systems: list[Target]
    profiles: dict[str, dict[str, Any]]
    placeholders: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()

    def target(self, target_id: str) -> Target:
        for item in self.systems:
            if item.id == target_id:
                return item
        raise KeyError(f"Unknown target {target_id!r}")

    def profile(self, name: str) -> dict[str, Any]:
        if name not in self.profiles:
            raise KeyError(f"Unknown profile {name!r}")
        return self.profiles[name]

    def capability(self, name: str) -> CapabilityProfile:
        if name not in self.capability_profiles:
            raise KeyError(f"Unknown capability profile {name!r}")
        return self.capability_profiles[name]

    def jump_host(self, name: str) -> JumpHostSpec:
        if name not in self.jump_hosts:
            raise KeyError(f"Unknown jump host {name!r}")
        return self.jump_hosts[name]


def default_config_dir() -> Path:
    env = os.environ.get("MGX_ARC_CONFIG_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config" / "mgx_arc"


def is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and PLACEHOLDER_TOKEN in value


def collect_placeholders(value: Any, found: list[str] | None = None) -> list[str]:
    found = found if found is not None else []
    if isinstance(value, str) and PLACEHOLDER_TOKEN in value:
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            collect_placeholders(item, found)
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_placeholders(item, found)
    return found


def resolve_credential(spec: CredentialSpec, environ: dict[str, str] | None = None) -> ResolvedCredential:
    env = environ if environ is not None else os.environ
    username = (env.get(spec.username_env) or "").strip()
    password = env.get(spec.password_env)
    if not username or password is None or password == "":
        raise ConfigError(
            f"Credential {spec.id!r} is missing environment values "
            f"{spec.username_env} and/or {spec.password_env}."
        )
    return ResolvedCredential(username=username, password=password)


def load_config(path: str | Path | None = None) -> MgxArcConfig:
    """Load and validate YAML from a file or the default config directory."""
    source = Path(path) if path is not None else default_config_dir()
    if source.is_dir():
        mapping, paths = _load_config_dir(source)
    else:
        mapping = _read_yaml_file(source)
        paths = (str(source),)
    return load_config_from_mapping(mapping, source_paths=paths)


def load_config_from_mapping(
    mapping: dict[str, Any],
    *,
    source_paths: tuple[str, ...] = (),
) -> MgxArcConfig:
    if not isinstance(mapping, dict):
        raise ConfigError("Configuration root must be a mapping.")
    _reject_plaintext_secrets(mapping)
    errors: list[str] = []
    version = mapping.get("version")
    if version != SUPPORTED_VERSION:
        errors.append(f"Unsupported config version {version!r}; expected {SUPPORTED_VERSION}.")

    credentials = _parse_credentials(mapping.get("credentials") or {}, errors)
    jump_hosts = _parse_jump_hosts(mapping.get("jump_hosts") or {}, credentials, errors)
    capability_profiles = _parse_capability_profiles(
        mapping.get("capability_profiles") or {}, errors
    )
    profiles = mapping.get("profiles") or {}
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        errors.append("profiles must be a mapping.")
        profiles = {}
    profiles = _apply_legacy_profile_adapters(profiles, source_paths)
    systems = _parse_systems(
        mapping.get("systems") or [],
        credentials=credentials,
        jump_hosts=jump_hosts,
        capability_profiles=capability_profiles,
        profiles=profiles,
        errors=errors,
    )
    if errors:
        raise ConfigError("Invalid MGX ARC configuration:\n- " + "\n- ".join(errors))
    placeholders = tuple(dict.fromkeys(collect_placeholders(mapping)))
    return MgxArcConfig(
        version=int(version),
        credentials=credentials,
        jump_hosts=jump_hosts,
        capability_profiles=capability_profiles,
        systems=systems,
        profiles=profiles,
        placeholders=placeholders,
        source_paths=source_paths,
    )


def _load_config_dir(directory: Path) -> tuple[dict[str, Any], tuple[str, ...]]:
    systems_path = directory / "systems.yaml"
    profiles_path = directory / "profiles.yaml"
    combined_path = directory / "config.yaml"
    paths: list[Path] = []
    merged: dict[str, Any] = {}
    if combined_path.is_file():
        merged = _read_yaml_file(combined_path)
        paths.append(combined_path)
    if systems_path.is_file():
        merged = _merge_maps(merged, _read_yaml_file(systems_path), systems_path)
        paths.append(systems_path)
    if profiles_path.is_file():
        merged = _merge_maps(merged, _read_yaml_file(profiles_path), profiles_path)
        paths.append(profiles_path)
    if not paths:
        raise ConfigError(
            f"No YAML configuration found in {directory}. "
            "Expected systems.yaml, profiles.yaml, or config.yaml."
        )
    return merged, tuple(str(p) for p in paths)


def _read_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    if yaml is None:
        raise ConfigError(
            "PyYAML is not installed; install dependencies from requirements.txt."
        )
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Configuration file {path} must contain a mapping.")
    return loaded


def _merge_maps(base: dict[str, Any], incoming: dict[str, Any], source: Path) -> dict[str, Any]:
    merged = dict(base)
    for key, value in incoming.items():
        if key == "version" and key in merged and merged[key] != value:
            raise ConfigError(
                f"Conflicting version values while merging {source}."
            )
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            overlap = set(merged[key]).intersection(value)
            combined = dict(merged[key])
            combined.update(value)
            if overlap and key in {"credentials", "jump_hosts", "capability_profiles", "profiles"}:
                for item in sorted(overlap):
                    if merged[key][item] != value[item]:
                        raise ConfigError(
                            f"Conflicting {key} entry {item!r} while merging {source}."
                        )
            merged[key] = combined
        else:
            merged[key] = value
    return merged


def _apply_legacy_profile_adapters(
    profiles: dict[str, Any],
    source_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Expose legacy JSON maps through profiles without replacing YAML values."""
    root = _legacy_config_root(source_paths)
    legacy = {
        "usb": {
            "golden_map": _read_optional_json(root / "usb_golden_map.json"),
        },
        "i2c": {
            "bus_map": _read_optional_json(root / "i2c_bus_map.json"),
            "expected_devices": _read_optional_json(root / "i2c_expected_devices.json"),
        },
        "inventory": {
            "os_inventory_map": _read_optional_json(root / "os_inventory_map.json"),
        },
    }
    adapted: dict[str, Any] = {}
    for profile_id, raw_profile in profiles.items():
        if not isinstance(raw_profile, dict):
            adapted[profile_id] = raw_profile
            continue
        profile = dict(raw_profile)
        for section_name, values in legacy.items():
            raw_section = profile.get(section_name)
            section = dict(raw_section) if isinstance(raw_section, dict) else {}
            for key, value in values.items():
                if value is not None and not section.get(key):
                    section[key] = value
            if section or section_name in profile:
                profile[section_name] = section
        adapted[profile_id] = profile
    return adapted


def _legacy_config_root(source_paths: tuple[str, ...]) -> Path:
    required = (
        "usb_golden_map.json",
        "i2c_bus_map.json",
        "i2c_expected_devices.json",
        "os_inventory_map.json",
    )
    candidates: list[Path] = []
    for source in source_paths:
        path = Path(source).resolve()
        candidates.extend((path.parent, *path.parents))
    candidates.append(Path(__file__).resolve().parent.parent)
    for candidate in candidates:
        if any((candidate / name).is_file() for name in required):
            return candidate
    return Path(__file__).resolve().parent.parent


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _reject_plaintext_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            here = f"{path}.{key}" if path else str(key)
            if key_l in SECRET_KEYS:
                raise ConfigError(
                    f"Refusing plaintext credential field {here!r}. "
                    "Use environment variable references (username_env / password_env)."
                )
            _reject_plaintext_secrets(item, here)
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            _reject_plaintext_secrets(item, f"{path}[{idx}]")


def _require_id(value: Any, label: str, errors: list[str]) -> str:
    text = str(value or "").strip()
    if not text or not ID_RE.match(text):
        errors.append(f"{label} is missing or not a valid id.")
    return text


def _parse_credentials(
    raw: Any, errors: list[str]
) -> dict[str, CredentialSpec]:
    out: dict[str, CredentialSpec] = {}
    if not isinstance(raw, dict):
        errors.append("credentials must be a mapping.")
        return out
    for cred_id, spec in raw.items():
        cid = str(cred_id).strip()
        if not ID_RE.match(cid):
            errors.append(f"Invalid credential id {cred_id!r}.")
            continue
        if not isinstance(spec, dict):
            errors.append(f"Credential {cid} must be a mapping.")
            continue
        user_env = str(spec.get("username_env") or "").strip()
        pass_env = str(spec.get("password_env") or "").strip()
        if not ENV_NAME_RE.match(user_env) or not ENV_NAME_RE.match(pass_env):
            errors.append(
                f"Credential {cid} needs username_env and password_env names."
            )
            continue
        extra = set(spec) - {"username_env", "password_env"}
        if extra:
            errors.append(f"Credential {cid} has unsupported keys: {sorted(extra)}.")
        out[cid] = CredentialSpec(id=cid, username_env=user_env, password_env=pass_env)
    return out


def _parse_jump_hosts(
    raw: Any,
    credentials: dict[str, CredentialSpec],
    errors: list[str],
) -> dict[str, JumpHostSpec]:
    out: dict[str, JumpHostSpec] = {}
    if not isinstance(raw, dict):
        errors.append("jump_hosts must be a mapping.")
        return out
    for jump_id, spec in raw.items():
        jid = str(jump_id).strip()
        if not jid:
            errors.append("Jump host id is empty.")
            continue
        if not isinstance(spec, dict):
            errors.append(f"Jump host {jid} must be a mapping.")
            continue
        host = str(spec.get("host") or "").strip()
        cred_ref = str(spec.get("credential_ref") or "").strip()
        port = spec.get("port", 22)
        if not host:
            errors.append(f"Jump host {jid} is missing host.")
        if cred_ref not in credentials and not is_placeholder(cred_ref):
            errors.append(f"Jump host {jid} references unknown credential {cred_ref!r}.")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            errors.append(f"Jump host {jid} has an invalid port.")
            port = 22
        placeholders = tuple(collect_placeholders({"id": jid, "host": host, "credential_ref": cred_ref}))
        out[jid] = JumpHostSpec(
            id=jid,
            host=host,
            credential_ref=cred_ref,
            port=int(port),
            placeholders=placeholders,
        )
    return out


def _parse_capability_profiles(
    raw: Any, errors: list[str]
) -> dict[str, CapabilityProfile]:
    out: dict[str, CapabilityProfile] = {}
    if not isinstance(raw, dict):
        errors.append("capability_profiles must be a mapping.")
        return out
    allowed_sources = {"bmc", "host"}
    for name, spec in raw.items():
        pid = str(name).strip()
        if not ID_RE.match(pid):
            errors.append(f"Invalid capability profile id {name!r}.")
            continue
        if not isinstance(spec, dict):
            errors.append(f"Capability profile {pid} must be a mapping.")
            continue
        sources = spec.get("usb_sources", ["bmc", "host"])
        if not isinstance(sources, list) or any(s not in allowed_sources for s in sources):
            errors.append(f"Capability profile {pid} has invalid usb_sources.")
            sources = ["bmc", "host"]
        out[pid] = CapabilityProfile(
            id=pid,
            redfish_power=bool(spec.get("redfish_power", True)),
            graceful_shutdown=bool(spec.get("graceful_shutdown", True)),
            ac_cycle=bool(spec.get("ac_cycle", False)),
            usb_sources=tuple(str(s) for s in sources),
            mctp=bool(spec.get("mctp", True)),
            i2c=bool(spec.get("i2c", True)),
            fru_redfish=bool(spec.get("fru_redfish", True)),
            fru_ipmi=bool(spec.get("fru_ipmi", False)),
            mcu_recovery=bool(spec.get("mcu_recovery", False)),
        )
    return out


def _parse_systems(
    raw: Any,
    *,
    credentials: dict[str, CredentialSpec],
    jump_hosts: dict[str, JumpHostSpec],
    capability_profiles: dict[str, CapabilityProfile],
    profiles: dict[str, Any],
    errors: list[str],
) -> list[Target]:
    if not isinstance(raw, list):
        errors.append("systems must be a list.")
        return []
    seen_ids: set[str] = set()
    seen_pairs: set[tuple[str, str]] = set()
    targets: list[Target] = []
    for idx, item in enumerate(raw):
        label = f"systems[{idx}]"
        if not isinstance(item, dict):
            errors.append(f"{label} must be a mapping.")
            continue
        target_id = _require_id(item.get("id"), f"{label}.id", errors)
        chassis_id = str(item.get("chassis_id") or "").strip()
        node_id = str(item.get("node_id") or "").strip()
        if not chassis_id or not node_id:
            errors.append(f"{label} needs chassis_id and node_id.")
        pair = (chassis_id, node_id)
        if target_id in seen_ids:
            errors.append(f"Duplicate target id {target_id!r}.")
        if chassis_id and node_id and pair in seen_pairs:
            errors.append(f"Duplicate chassis/node pair {pair!r}.")
        seen_ids.add(target_id)
        seen_pairs.add(pair)
        cred_ref = str(item.get("credential_ref") or "").strip()
        host_cred = item.get("host_credential_ref")
        host_cred_ref = str(host_cred).strip() if host_cred else None
        jump_ref_raw = item.get("jump_host_ref")
        jump_ref = str(jump_ref_raw).strip() if jump_ref_raw else None
        expected = str(item.get("expected_profile") or "").strip()
        capability = str(item.get("capability_profile") or "").strip()
        if cred_ref not in credentials:
            errors.append(f"{label} references unknown credential {cred_ref!r}.")
        if host_cred_ref and host_cred_ref not in credentials:
            errors.append(f"{label} references unknown host credential {host_cred_ref!r}.")
        if jump_ref and jump_ref not in jump_hosts:
            errors.append(f"{label} references unknown jump host {jump_ref!r}.")
        if expected and expected not in profiles:
            errors.append(f"{label} references unknown expected_profile {expected!r}.")
        if capability and capability not in capability_profiles:
            errors.append(f"{label} references unknown capability_profile {capability!r}.")
        tags = item.get("tags") or []
        if tags and not isinstance(tags, list):
            errors.append(f"{label}.tags must be a list.")
            tags = []
        placeholders = tuple(collect_placeholders(item))
        targets.append(
            Target(
                id=target_id,
                chassis_id=chassis_id,
                node_id=node_id,
                label=str(item.get("label") or target_id),
                bmc_host=str(item.get("bmc_host") or "").strip(),
                os_host=str(item.get("os_host") or "").strip(),
                credential_ref=cred_ref,
                host_credential_ref=host_cred_ref,
                jump_host_ref=jump_ref or None,
                tags=tuple(str(t) for t in tags),
                revision=str(item.get("revision") or ""),
                expected_profile=expected,
                capability_profile=capability,
                placeholders=placeholders,
            )
        )
    return targets
