#!/usr/bin/env python3
"""Bundle the whole tmv71-remote source tree into one Markdown snapshot.

A local backup / hand-off artefact: every text source file concatenated into a
single Markdown document (tree + fenced code blocks). The output
(PROJECT-SNAPSHOT.md at the repo root) is gitignored and must never be committed
or published — only this generator lives in the repo.

Run:  python docs/gen_snapshot.py   (or: backend/.venv/bin/python docs/gen_snapshot.py)
"""
import os
import re
import datetime

# repo root = parent of this file's docs/ directory (portable, not hard-coded)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "PROJECT-SNAPSHOT.md")

INCLUDE_EXT = {".py", ".js", ".html", ".css", ".md", ".txt", ".sh",
               ".service", ".toml", ".cfg", ".ini", ".yml", ".yaml"}
INCLUDE_NAMES = {"requirements.txt", "Dockerfile", "docker-compose.yml"}
# runtime data / the snapshot itself are not source and stay out
EXCLUDE_FILES = {"runtime.json", "logbook.json", "PROJECT-SNAPSHOT.md"}
EXCLUDE_DIRS = {".venv", "node_modules", ".git", "__pycache__", "models",
                "certs", "branding", "licenses"}
LANG = {".py": "python", ".js": "javascript", ".html": "html", ".css": "css",
        ".md": "markdown", ".json": "json", ".sh": "bash", ".service": "ini",
        ".toml": "toml", ".cfg": "ini", ".ini": "ini", ".yml": "yaml",
        ".yaml": "yaml", ".txt": "text"}


def wanted(name):
    if name in EXCLUDE_FILES:
        return False
    return name in INCLUDE_NAMES or os.path.splitext(name)[1].lower() in INCLUDE_EXT


def collect():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if wanted(fn):
                files.append(os.path.join(dirpath, fn))
    return sorted(files)


def fence_for(text):
    """A backtick fence longer than any run inside the file, so nothing breaks."""
    longest = max((len(m.group()) for m in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def tree(files):
    lines, seen = [], set()
    for f in files:
        parts = os.path.relpath(f, ROOT).split(os.sep)
        for i in range(len(parts)):
            key = os.sep.join(parts[:i + 1])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"{'  ' * i}- {parts[i]}")
    return "\n".join(lines)


def main():
    files = collect()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    total_lines, parts = 0, []
    for f in files:
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as exc:  # noqa: BLE001
            text = f"<< konnte nicht gelesen werden: {exc} >>"
        rel = os.path.relpath(f, ROOT)
        lang = LANG.get(os.path.splitext(f)[1].lower(), "")
        fence = fence_for(text)
        total_lines += text.count("\n") + 1
        parts.append(f"## `{rel}`\n\n{fence}{lang}\n{text}\n{fence}\n")

    header = (
        f"# TM-V71 Remote — Projekt-Snapshot\n\n"
        f"> Vollständiger Quellcode-Export. **Lokales Backup — nicht ins Git-Repo committen "
        f"/ nicht auf GitHub veröffentlichen** (per `.gitignore` ausgeschlossen).\n\n"
        f"- Erzeugt: {now}\n"
        f"- Dateien: {len(files)}\n"
        f"- Zeilen (Quellcode): {total_lines}\n"
        f"- Ausgeschlossen: `.venv/`, `models/`, `certs/`, `branding/`, "
        f"`node_modules/`, `.git/`, `runtime.json`, `logbook.json`, Binär-/Mediendateien\n\n"
        f"---\n\n## Dateibaum\n\n{tree(files)}\n\n---\n\n"
    )
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(parts))
    print("geschrieben:", OUT)
    print("Dateien:", len(files), "Zeilen:", total_lines)


if __name__ == "__main__":
    main()
