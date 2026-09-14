# Contributing to MGX ARC GUI

Thank you for helping improve the MGX ARC GUI. This project is a Flask + vanilla JavaScript web app with no frontend build step — keep changes simple and focused.

## Team access model

- **Users** always open the shared Spark instance: **http://10.110.33.21:4281/**
- **Contributors** change code in Git, open a PR, and wait for merge.
- **Maintainers** deploy merged changes to Spark ([deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md)).

Do **not** ask teammates to run `python app.py` on their laptops for daily BMC work. Do **not** distribute ad-hoc local copies of the GUI.

## Getting started (contributors)

1. **Fork** the repository: https://github.com/riddhigadd/mgx-arc-spt-validation
2. **Clone** your fork locally.
3. Create a **virtual environment** and install dependencies (for local smoke-testing only):

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

4. Optional — smoke-test locally before opening a PR (maintainers only; not for team use):

   ```powershell
   python app.py
   ```

   Opens http://localhost:4282/ on your machine. Connect to a BMC you are authorized to use. After validation, deploy via the maintainer workflow — do not share this localhost URL with the team.

5. Create a **feature branch** from `main`:

   ```bash
   git checkout -b feature/my-change
   ```

## Pull requests

- Keep PRs **focused** — one logical change per PR when possible.
- Describe **what** changed and **why** in the PR body.
- Test against a live BMC (on Spark after deploy, or locally for pre-merge smoke tests) — document manual test steps when automated tests are not applicable.
- Do not include unrelated refactors or formatting-only churn.

## After your PR is merged

1. A **maintainer** pulls the latest `main` and deploys to Spark:

   ```powershell
   $env:SPARK_HOST = "10.110.33.21"
   $env:SPARK_USER = "sgaddamwar"
   $env:SPARK_PASSWORD = "<from env>"
   python deploy/push_to_spark.py
   ```

2. The team uses **http://10.110.33.21:4281/** — no action required on their laptops except a hard refresh (Ctrl+F5) if front-end files changed.

See [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md) for the full deploy and verification checklist.

## Code style

Match the existing codebase:

### Python (Flask)

- Python 3.9+ compatible syntax.
- Follow patterns in `app.py` and `backend/` — thin route handlers, helpers for BMC/SSH/Redfish logic.
- Use environment variables for secrets and tunable timeouts (see `backend/config.py`).
- Fleet Health routes live in `backend/api.py` as a Flask Blueprint under `/api/arc`.

### JavaScript

- **Vanilla JS** only — no React, Vue, or bundlers.
- `"use strict"` at the top of `static/js/app.js`.
- DOM helpers: `$`, `$$`, `el()` — reuse them rather than adding a framework.
- Tab loaders: add an entry to the `loaders` map in `switchTab()` and a matching `#panel-*` div in `index.html`.
- API calls go through `apiFetch()` with BMC headers (`X-BMC-IP`, `X-BMC-User`, `X-BMC-Pass`).

### CSS

- Theme tokens live in `:root` in `static/css/style.css` — prefer CSS variables over hard-coded colors.
- Fleet Health styles are in `static/css/health-console.css`.

### YAML / JSON config

- **Never** put passwords in YAML. Use `username_env` / `password_env` references only.
- Lab placeholders use the `TODO_MGX_ARC_*` prefix — replace with real values on Spark via env vars; do not commit real hostnames or credentials.

## What not to commit

| Never commit | Why |
| ------------ | --- |
| `.env`, `.flaskenv` | Local secrets |
| `.venv/`, `__pycache__/` | Generated / local |
| `firmware_images/*.{bin,fwpkg,image,img}` | Large binaries |
| `deploy_backup/` | Remote deploy snapshots |
| `*.db`, `data/*.db` | Local snapshot databases |
| Real BMC/host passwords | Use env vars |
| `SPARK_PASSWORD` or deploy credentials | Environment-only |
| OneDrive sync artifacts | `.tmp`, conflict copies |
| `.cursor/` | Editor-local |

See [.gitignore](.gitignore) for the full list.

## Testing

```powershell
pip install -r requirements.txt
pytest
```

Most features require a live MGX ARC BMC on the lab network. After deploy, verify on **http://10.110.33.21:4281/**. Document your manual test steps in the PR when automated tests are not applicable.

## Security

Read [SECURITY.md](SECURITY.md) before submitting changes that touch authentication, credential handling, or network exposure.

## Questions

Open a GitHub issue for bugs, feature requests, or questions about customizing the GUI — see [CUSTOMIZATION.md](CUSTOMIZATION.md) for common extension points.
