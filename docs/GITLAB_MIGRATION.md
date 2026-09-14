# GitLab migration (GitHub → NVIDIA GitLab)

Move the transitional GitHub repo to an **NVIDIA GitLab group** so ITSS VM deploys clone from org-owned source.

**You must create the GitLab project in the browser first** — this doc provides paste-ready commands once you have the URL.

---

## 1. Create GitLab project (browser)

1. Open [NVIDIA GitLab](https://gitlab-master.nvidia.com/) (or your org’s GitLab host).
2. **New project** → **Create blank project** (or import from GitHub if your group allows).
3. Suggested settings:
   - **Project name:** `mgx-arc-gui` (or `mgx-arc-spt-validation` to match GitHub)
   - **Visibility:** Internal / group-private per platform team policy
   - **Initialize with README:** **No** (we push existing history)
4. Copy the **HTTPS clone URL**, e.g.  
   `https://gitlab-master.nvidia.com/<group>/mgx-arc-gui.git`

Record: `GITLAB_URL=<paste URL here>`

---

## 2. Add GitLab remote and push mirror (workstation)

From your local clone (same tree as GitHub `main`):

```bash
cd /path/to/mgx-arc-spt-validation

# Add org remote (keep origin = GitHub until cutover)
git remote add gitlab GITLAB_URL

# Verify remotes
git remote -v

# Push all branches and tags
git push gitlab --all
git push gitlab --tags

# Optional: make GitLab the default push target
git remote set-url origin GITLAB_URL
git remote rename origin github
git remote rename gitlab origin
```

Replace `GITLAB_URL` with the URL from step 1.

---

## 3. Update documentation links (maintainer)

After the project exists, update placeholders in:

| File | Change |
| ---- | ------ |
| [README.md](../README.md) | Clone URL → GitLab |
| [DEPLOYMENT.md](../DEPLOYMENT.md) | `git clone` example |
| [HANDOFF.md](../HANDOFF.md) | Source / target repo table |
| [MANUAL_STEPS.md](../MANUAL_STEPS.md) | Transfer checklist item |

Example clone line for ITSS VM:

```bash
git clone https://gitlab-master.nvidia.com/<group>/mgx-arc-gui.git /opt/mgx-arc-gui
```

---

## 4. CI / deploy keys (if used)

- Rotate any **personal GitHub tokens** used for deploy; issue **group deploy keys** or CI variables on GitLab.
- Re-point CI pipelines from `.github/` to GitLab CI if/when added.
- Revoke old GitHub deploy credentials after cutover ([MANUAL_STEPS.md](../MANUAL_STEPS.md)).

---

## 5. Decommission GitHub (after sign-off)

1. Confirm ITSS VM and team clone from GitLab only.
2. Archive or delete [riddhigadd/mgx-arc-spt-validation](https://github.com/riddhigadd/mgx-arc-spt-validation) per org policy.
3. Add a short README redirect on GitHub if archive must stay visible.

---

## Quick reference

```bash
# One-liner after GITLAB_URL is known:
git remote add gitlab GITLAB_URL && git push gitlab --all && git push gitlab --tags
```

See also [HANDOFF_NOW.md](../HANDOFF_NOW.md) for the full handoff checklist.
