# SSO integration (Azure AD / NVIDIA OIDC)

This document describes how to protect the MGX ARC GUI with **NVIDIA SSO** (Azure AD) using **OIDC Authorization Code flow with PKCE** — the standard pattern for internal web applications.

> **Integration status:** SSO middleware is **not implemented in the application code yet**. Today the GUI relies on network placement (VPN/lab LAN) and BMC credential validation. Follow the manual registration steps below, then add a future PR to enforce OIDC at the Flask layer (see [Where to wire auth](#where-to-wire-auth-in-flask)).

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
| `OIDC_REQUIRE_AUTH` | Future flag: reject requests without valid session |

Do **not** commit real values. Store in `/opt/mgx-arc-gui/.env` or a systemd `EnvironmentFile`.

---

## 4. Where to wire auth in Flask

Suggested integration points (future PR):

1. **`app.py`** — after `app = Flask(...)`, register an `@app.before_request` handler that:
   - Allows static assets and the OAuth callback path without a session.
   - Redirects unauthenticated browser requests to the OIDC authorize URL (PKCE code challenge).
   - Validates ID/access tokens on callback and stores user identity in a signed server-side session (Flask `session` + `SECRET_KEY` from env).

2. **`wsgi.py`** — no change required if middleware lives in `app.py`; gunicorn loads `wsgi:app`.

3. **nginx** — terminate TLS; proxy to gunicorn on `127.0.0.1:4281`. Optionally add `auth_request` only if using nginx-level OIDC; Flask middleware is simpler for this app.

Libraries commonly used: `Authlib` or `msal` + `requests`. Follow NVIDIA security review for session cookies (`Secure`, `HttpOnly`, `SameSite`).

4. **API routes** — decide policy for `/api/bmc` and `/api/arc`:
   - **Recommended:** require the same OIDC session for all browser-initiated API calls; reject cross-origin anonymous use.
   - BMC credentials in headers remain separate (Direct Connect); SSO protects *who can open the GUI*, not BMC auth.

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
