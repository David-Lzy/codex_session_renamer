#!/usr/bin/env python3
"""Install this repository as a Codex skill.

The installer is intentionally dependency-free and works on Windows, macOS,
and Linux. It copies the repository into CODEX_HOME/skills/codex-session-emoji.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
from pathlib import Path


DEFAULT_SKILL_NAME = "codex-session-emoji"
EXCLUDED_NAMES = {
    ".git",
    ".github",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "venv",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip"}


def default_codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".codex"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def ignore_names(_directory: str, names: list[str]) -> set[str]:
    ignored = set()
    for name in names:
        path_name = Path(name)
        if name in EXCLUDED_NAMES or path_name.suffix in EXCLUDED_SUFFIXES:
            ignored.add(name)
    return ignored


def install(source: Path, destination: Path, force: bool) -> tuple[Path, Path | None]:
    source = source.resolve()
    destination = destination.resolve()
    if source == destination:
        return destination, None
    if not (source / "SKILL.md").exists():
        raise SystemExit(f"SKILL.md not found in source: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    backup = None
    if destination.exists():
        if not force:
            raise SystemExit(f"Destination already exists: {destination}\nRerun with --force to replace it safely.")
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = destination.with_name(f"{destination.name}.backup-{stamp}")
        shutil.move(str(destination), str(backup))
    shutil.copytree(source, destination, ignore=ignore_names)
    return destination, backup


def main() -> int:
    parser = argparse.ArgumentParser(description="Install codex-session-emoji into CODEX_HOME/skills.")
    parser.add_argument("--codex-home", default=str(default_codex_home()), help="Codex home directory. Defaults to CODEX_HOME or ~/.codex.")
    parser.add_argument("--name", default=DEFAULT_SKILL_NAME, help="Skill directory name under CODEX_HOME/skills.")
    parser.add_argument("--source", default=str(repo_root()), help="Repository/skill source directory.")
    parser.add_argument("--force", action="store_true", help="Back up and replace an existing installed skill.")
    args = parser.parse_args()

    codex_home = Path(args.codex_home).expanduser()
    source = Path(args.source).expanduser()
    destination = codex_home / "skills" / args.name
    installed, backup = install(source, destination, args.force)
    print(f"Installed skill: {installed}")
    if backup:
        print(f"Previous install backed up to: {backup}")
    print("Try: python scripts/session_renamer.py quickstart")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
