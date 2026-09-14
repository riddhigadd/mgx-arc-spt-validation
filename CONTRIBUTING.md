# Contributing to MGX ARC GUI

Thank you for helping improve the MGX ARC GUI. This project is a Flask + vanilla JavaScript web app with no frontend build step — keep changes simple and focused.

## How to contribute

You can:

- **Fork** the repo, make changes, and open a **pull request** upstream
- **Deploy your fork** on your own laptop, lab VM, or server — no shared host required
- **Run locally** to test before submitting a PR

There is no requirement to use any particular hosted instance. Deploy wherever you have BMC network access.

## Getting started

1. **Fork** the repository: https://github.com/riddhigadd/mgx-arc-spt-validation
2. **Clone** your fork locally.
3. Create a **virtual environment** and install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

4. **Run locally** to test your changes:

   ```powershell
   python app.py
   ```

   Opens http://localhost:4282/ on your machine. Connect to a BMC you are authorized to use.

5. Create a **feature branch** from `main`:

   ```bash
   git checkout -b feature/my-change
   ```

## Pull requests

- Keep PRs **focused** — one logical change per PR when possible.
- Describe **what** changed and **why** in the PR body.
- Test against a live BMC (locally or on your deployed server) — document manual test steps when automated tests are not applicable.
- Do not include unrelated refactors or formatting-only churn.

## After your PR is merged

Deploy the updated code on **your** host (or ask your team maintainer to deploy on theirs):

```bash
git pull origin main
pip install -r requirements.txt
sudo systemctl restart mgx-arc-gui   # if using systemd
```

Or follow the full server guide in [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md).

Hard-refresh the browser (Ctrl+F5) if front-end files changed.

### Optional: push to a remote host via SFTP

If you maintain a remote server and use the included deploy helper:

```powershell
$env:SPARK_HOST = "<your-server-ip>"
$env:SPARK_USER = "<ssh-user>"
$env:SPARK_PASSWORD = "<from env>"
python deploy/push_to_spark.py
```

The script name references a lab Spark host historically; it works with any SSH-accessible server. See [deploy/DEPLOYMENT.md](deploy/DEPLOYMENT.md).

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
- Lab placeholders use the `TODO_MGX_ARC_*` prefix — replace with real values via env vars on your server; do not commit real hostnames or credentials.

## What not to commit

| Never commit | Why |
| ------------ | --- |
| `.env`, `.flaskenv` | Local secrets |
| `.venv/`, `__pycache__/` | Generated / local |
| `firmware_images/*.{bin,fwpkg,image,img}` | Large binaries |
| `deploy_backup/` | Remote deploy snapshots |
| `*.db`, `data/*.db` | Local snapshot databases |
| Real BMC/host passwords | Use env vars |
| SSH/deploy passwords | Environment-only |
| OneDrive sync artifacts | `.tmp`, conflict copies |
| `.cursor/` | Editor-local |

See [.gitignore](.gitignore) for the full list.

## Testing

```powershell
pip install -r requirements.txt
pytest
```

Most features require a live MGX ARC BMC on the lab network. Test locally with `python app.py` or on your deployed server. Document your manual test steps in the PR when automated tests are not applicable.

## Security

Read [SECURITY.md](SECURITY.md) before submitting changes that touch authentication, credential handling, or network exposure.

## Questions

Open a GitHub issue for bugs, feature requests, or questions about customizing the GUI — see [CUSTOMIZATION.md](CUSTOMIZATION.md) for common extension points.
