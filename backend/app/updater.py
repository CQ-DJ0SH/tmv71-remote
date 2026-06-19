"""Self-update from the git origin (settings ▸ General ▸ Software update).

The deployment is a git checkout; the update pulls the latest commit from the
configured remote and restarts the systemd service. LAN-only use.
"""
from __future__ import annotations

import logging
import os
import subprocess

log = logging.getLogger("tmv71.update")

# repo root = two levels up from this package (…/<repo>/backend/app/updater.py)
REPO_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SERVICE = "tmv71-remote.service"


def _git(*args: str, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", REPO_DIR, *args],
                          capture_output=True, text=True, timeout=timeout)


def head_date() -> str | None:
    """ISO date (YYYY-MM-DD) of the current HEAD commit — the build date of
    this checkout. None if the deployment isn't a git repo."""
    try:
        r = _git("log", "-1", "--format=%cd", "--date=short", timeout=10)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def status() -> dict:
    """Fetch and report how far behind/ahead the checkout is vs the remote."""
    if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return {"is_repo": False, "dir": REPO_DIR}
    fetch = _git("fetch", "--quiet")
    cur = _git("rev-parse", "--short", "HEAD").stdout.strip()
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    behind_p = _git("rev-list", "--count", "HEAD..@{u}")
    ahead_p = _git("rev-list", "--count", "@{u}..HEAD")
    behind = int(behind_p.stdout.strip() or 0) if behind_p.returncode == 0 else 0
    ahead = int(ahead_p.stdout.strip() or 0) if ahead_p.returncode == 0 else 0
    changes = _git("log", "--oneline", "-10", "HEAD..@{u}").stdout.strip()
    dirty = bool(_git("status", "--porcelain").stdout.strip())
    return {
        "is_repo": True, "branch": branch, "current": cur,
        "behind": behind, "ahead": ahead, "changes": changes, "dirty": dirty,
        "fetch_error": fetch.stderr.strip() if fetch.returncode != 0 else "",
    }


def apply() -> dict:
    """Update the checkout to the remote tip, then restart the service.

    A deployment is a pure consumer of the remote, so rather than a fast-forward
    pull (which aborts once the local branch has diverged — e.g. a locally made
    or amended commit: "Diverging branches can't be fast-forwarded"), we fetch
    and hard-reset onto the upstream tip. Tracked files are forced to match the
    remote; gitignored runtime files (runtime.json, certs/, branding/, .venv)
    are left untouched. Restart is detached so it survives after the response.
    """
    fetch = _git("fetch", "--quiet", timeout=180)
    if fetch.returncode != 0:
        out = (fetch.stdout + fetch.stderr).strip()
        log.error("update fetch failed: %s", out)
        return {"ok": False, "output": out or "git fetch failed"}
    # resolve the configured upstream (e.g. origin/main); fall back to origin/<branch>
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    upstream = up.stdout.strip()
    if not upstream:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "main"
        upstream = f"origin/{branch}"
    reset = _git("reset", "--hard", upstream, timeout=180)
    out = (reset.stdout + reset.stderr).strip()
    ok = reset.returncode == 0
    if ok:
        # restart after we've returned the HTTP response (new session survives us)
        subprocess.Popen(["sh", "-c", f"sleep 1; systemctl restart {SERVICE}"],
                         start_new_session=True)
        log.info("update applied (reset to %s); scheduling service restart", upstream)
    else:
        log.error("update reset failed: %s", out)
    return {"ok": ok, "output": out}
