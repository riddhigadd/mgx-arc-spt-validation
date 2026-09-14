# SSO integration (Azure AD / NVIDIA OIDC)

This document describes how to protect the MGX ARC GUI with **NVIDIA SSO** (Azure AD) using **OIDC Authorization Code flow with PKCE** — the standard pattern for internal web applications.

> **Integration status:** An **env-gated OIDC scaffold** ships in `backend/sso.py` (`SSO_ENABLED=false` by default). Lab/Spark deploys work unchanged. After ITSS app registration, set `SSO_ENABLED=true` and OIDC vars in `.env` — see [HANDOFF_NOW.md](../HANDOFF_NOW.md).

---

## Prerequisites

- Internal HTTPS URL for the GUI (nginx TLS termination + corporate cert). See [DEPLOYMENT.md](../DEPLOYMENT.md).
- VM hostname and DNS entry (corporate DNS — manual step).
- Owner access to ITSS Applications and Azure AD app assignment.

---

## 1. Register the application (ITSS)

1. Open [ITSS Applications](https://itss.nvidia.com/#/applications/).
2. Create or request a new application registration per internal KB **KB0028542** (OIDC Authorization Code + PKCE for web apps).
3. Record:
   - **Client ID**
   - **Tenant ID** (Azure AD directory)
   - **Redirect URI** — must match production URL exactly, e.g.  
     `https://mgx-arc-gui.<corp-hostname>.nvidia.com/oauth2/callback`
4. Scopes: at minimum `openid`, `profile`, `email` (adjust per ITSS/Azure template).

Reference: [ITSS Applications portal](https://itss.nvidia.com/#/applications/)

---

## 2. Assign users and groups (Azure AD)

After ITSS registration, assign who may sign in:

- [Azure AD — Enterprise applications](https://portal.azure.com/?feature.msaljs=true#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview/menuId~/null)

Add NVIDIA security groups (e.g. lab/platform team) — not individual ad-hoc accounts when avoidable.

---

## 3. Environment variables

Copy placeholders from [`.env.example`](../.env.example) on the server:

| Variable | Purpose |
| -------- | ------- |
| `OIDC_CLIENT_ID` | Application (client) ID from ITSS |
| `OIDC_CLIENT_SECRET` | Only if registered as confidential client; PKCE public clients often omit |
| `OIDC_TENANT_ID` | Azure AD tenant GUID |
| `OIDC_AUTHORITY` | e.g. `https://login.microsoftonline.com/<tenant-id>` |
| `OIDC_REDIRECT_URI` | Must match ITSS registration |
| `OIDC_SCOPES` | e.g. `openid profile email` |
| `SSO_ENABLED` | Master switch (`false` default; set `true` after ITSS registration) |
| `SECRET_KEY` | Flask session signing key (required when SSO enabled) |

Do **not** commit real values. Store in `/opt/mgx-arc-gui/.env` or a systemd `EnvironmentFile`.

---

## 4. Application wiring (implemented)

| Component | Behavior |
| --------- | -------- |
| `backend/sso.py` | Authlib OIDC + PKCE; `/oauth2/login`, `/oauth2/callback`, `/oauth2/logout` |
| `app.py` | Calls `init_sso(app)` after blueprint registration |
| `wsgi.py` | Unchanged — gunicorn loads `wsgi:app` |
| nginx | TLS termination; proxy to `127.0.0.1:4281` — see `deploy/nginx-mgx-arc.conf.example` |

When `SSO_ENABLED=true`:

- Static assets and `/oauth2/*` are public.
- Unauthenticated browser requests → redirect to NVIDIA login.
- `/api/*` without session → `401 JSON`.
- Session cookies: `Secure`, `HttpOnly`, `SameSite=Lax`.

BMC credentials in API headers remain separate; SSO controls *who can open the GUI*.

---

## 5. Validation

After implementation:

1. Unauthenticated browser → redirect to NVIDIA login.
2. User not in assigned Azure AD group → access denied.
3. Authenticated user → GUI loads; BMC connect still prompts for or uses configured BMC creds.
4. Follow post-deploy browser access checks: [ServiceNow KB0029440](https://nvidia.service-now.com/esc?id=kb_article&sysparm_article=KB0029440).

---

## Related docs

- [DEPLOYMENT.md](../DEPLOYMENT.md) — VM, HTTPS, nginx
- [HANDOFF.md](../HANDOFF.md) — owner checklist
- [MANUAL_STEPS.md](../MANUAL_STEPS.md) — steps that cannot be automated in git
