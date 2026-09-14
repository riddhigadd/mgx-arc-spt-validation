"""NVIDIA SSO (Azure AD OIDC) — env-gated scaffold.

Set ``SSO_ENABLED=true`` and OIDC_* vars from ITSS registration to enforce login.
When ``SSO_ENABLED=false`` (default), this module is a no-op so Spark/lab deploys
work without Azure AD.
"""

from __future__ import annotations

import logging
import os
import secrets
from typing import TYPE_CHECKING

from flask import jsonify, redirect, request, session, url_for

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/healthz"})
PUBLIC_PREFIXES = ("/static/",)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


def sso_enabled() -> bool:
    return _env_bool("SSO_ENABLED", False)


def _is_public_path(path: str) -> bool:
    if path in PUBLIC_PATHS:
        return True
    if path.startswith("/oauth2/"):
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def init_sso(app: Flask) -> None:
    """Register OIDC routes and auth middleware when SSO_ENABLED=true."""
    if not sso_enabled():
        logger.debug("SSO disabled (SSO_ENABLED=false); skipping OIDC middleware")

        @app.route("/healthz")
        def healthz_disabled():
            return jsonify({"ok": True, "sso": False})

        return

    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    tenant_id = os.environ.get("OIDC_TENANT_ID", "").strip()
    if not client_id or not tenant_id:
        raise RuntimeError(
            "SSO_ENABLED=true but OIDC_CLIENT_ID or OIDC_TENANT_ID is missing. "
            "Set them in .env after ITSS app registration (see docs/SSO.md)."
        )

    try:
        from authlib.integrations.flask_client import OAuth
    except ImportError as exc:
        raise RuntimeError(
            "SSO_ENABLED=true requires Authlib. Install with: pip install Authlib"
        ) from exc

    authority = os.environ.get(
        "OIDC_AUTHORITY", f"https://login.microsoftonline.com/{tenant_id}"
    ).rstrip("/")
    redirect_uri = os.environ.get("OIDC_REDIRECT_URI", "").strip()
    scopes = os.environ.get("OIDC_SCOPES", "openid profile email")
    client_secret = os.environ.get("OIDC_CLIENT_SECRET", "").strip() or None

    secret_key = (
        os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or ""
    ).strip()
    if not secret_key:
        secret_key = secrets.token_hex(32)
        logger.warning(
            "SECRET_KEY not set; using ephemeral key (sessions reset on restart)"
        )
    app.config["SECRET_KEY"] = secret_key
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    oauth = OAuth(app)
    oauth.register(
        name="nvidia_sso",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{authority}/v2.0/.well-known/openid-configuration",
        client_kwargs={"scope": scopes, "code_challenge_method": "S256"},
    )

    def _callback_uri() -> str:
        if redirect_uri:
            return redirect_uri
        return url_for("oauth_callback", _external=True)

    @app.route("/healthz")
    def healthz():
        return jsonify({"ok": True, "sso": True})

    @app.route("/oauth2/login")
    def oauth_login():
        return oauth.nvidia_sso.authorize_redirect(_callback_uri())

    @app.route("/oauth2/callback")
    def oauth_callback():
        token = oauth.nvidia_sso.authorize_access_token()
        userinfo = token.get("userinfo")
        if not userinfo:
            userinfo = oauth.nvidia_sso.parse_id_token(token)
        session["user"] = {
            "email": userinfo.get("email") or userinfo.get("preferred_username"),
            "name": userinfo.get("name"),
            "sub": userinfo.get("sub"),
        }
        session.permanent = True
        return redirect("/")

    @app.route("/oauth2/logout")
    def oauth_logout():
        session.pop("user", None)
        return redirect("/")

    @app.before_request
    def require_sso_session():
        if _is_public_path(request.path):
            return None
        if session.get("user"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Authentication required. Sign in via SSO."}), 401
        return redirect(url_for("oauth_login"))

    logger.info("SSO enabled — OIDC middleware active (tenant %s…)", tenant_id[:8])
