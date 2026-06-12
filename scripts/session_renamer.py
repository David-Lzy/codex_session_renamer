#!/usr/bin/env python3
"""Review-first Codex session renaming helper.

The script prepares data for a Codex agent. It never renames sessions itself.
Actual renames must be applied by Codex through codex_app.set_thread_title.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import html
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any


PROPOSER_VERSION = "session-renamer-v2-descriptive"
DEFAULT_VLLM_BASE_URL = os.environ.get("SESSION_RENAMER_OPENAI_BASE_URL", "http://127.0.0.1:8000/v1")
DEFAULT_VLLM_API_KEY = "local-vllm"
DEFAULT_RETENTION_DAYS = 60
DEFAULT_BACKUP_KEEP = 20
DEFAULT_SUBAGENT_CHUNK_SIZE = 25
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
CODEX_SUBAGENT_MODELS = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex",
    "gpt-5.3-codex-spark",
}
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,79}$")

EMOJI_HINTS = [
    ("gmail", "📩"),
    ("email", "📩"),
    ("mail", "📩"),
    ("asr", "🎙️"),
    ("qwen", "🎙️"),
    ("audio", "🎙️"),
    ("voice", "🎙️"),
    ("supabase", "🗄️"),
    ("database", "🗄️"),
    ("dashboard", "📊"),
    ("tiktok", "📊"),
    ("video", "🎬"),
    ("downloadvideos", "🎬"),
    ("mp4", "🎞️"),
    ("game", "🎮"),
    ("parsec", "🎮"),
    ("docker", "🐳"),
    ("nordvpn", "🔀"),
    ("proxy", "🔀"),
    ("ssh", "🔐"),
    ("rdp", "🖥️"),
    ("server", "🖥️"),
    ("home", "🏠"),
    ("blog", "📝"),
    ("docs", "📘"),
    ("doc", "📘"),
    ("review", "🔎"),
    ("需求", "📋"),
    ("目录", "🗂️"),
    ("复制", "📦"),
    ("字体", "🔤"),
    ("终端", "✨"),
    ("错误", "❓"),
    ("排查", "🔎"),
]

KNOWN_EMOJI = {
    "📩",
    "🎙️",
    "🗄️",
    "📊",
    "🎬",
    "🎞️",
    "🎮",
    "🐳",
    "🔀",
    "🔐",
    "🖥️",
    "🏠",
    "📝",
    "📘",
    "🔎",
    "📋",
    "🗂️",
    "📦",
    "🔤",
    "✨",
    "❓",
    "🧯",
    "🔌",
    "🧰",
    "🧩",
    "🔄",
    "🧭",
    "🏷️",
    "🛠️",
    "📧",
    "🔒",
    "🌐",
    "📣",
    "🦞",
}

EMOJI_PREFIX_RE = re.compile("^[\U0001F000-\U0001FAFF]\ufe0f?\\s")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()


def skill_dir() -> Path:
    return Path(__file__).resolve().parents[1]


def paths(home: Path | None = None) -> dict[str, Path]:
    root = (home or codex_home()).resolve()
    cache_root = root / "cache" / "session-renamer"
    tmp_root = root / "tmp" / "session-renamer"
    return {
        "codex_home": root,
        "cache_root": cache_root,
        "tmp_root": tmp_root,
        "current": tmp_root / "current",
        "runs": cache_root / "runs",
        "backups": cache_root / "backups",
        "cache": cache_root / "cache.json",
        "apply_logs": cache_root / "apply_logs",
    }


def ensure_dirs(p: dict[str, Path]) -> None:
    for key in ("cache_root", "tmp_root", "current", "runs", "backups", "apply_logs"):
        p[key].mkdir(parents=True, exist_ok=True)


def assert_under(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    root = parent.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"Refusing unsafe path outside {root}: {resolved}")
    return resolved


def read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def ssh_config_hosts(config_path: Path | None = None) -> list[str]:
    path = config_path or (Path.home() / ".ssh" / "config")
    if not path.exists():
        return []
    hosts: list[str] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            match = re.match(r"(?i)^host\s+(.+)$", stripped)
            if not match:
                continue
            for host in match.group(1).split():
                if any(ch in host for ch in "*?[]!") or host.lower() == "localhost":
                    continue
                if host not in seen:
                    seen.add(host)
                    hosts.append(host)
    return hosts


def sanitize_remote_hosts(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = re.split(r"[\s,;]+", value)
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = []
    hosts: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        host = item.strip()
        if not host or any(ch.isspace() for ch in host):
            continue
        if re.fullmatch(r"[A-Za-z0-9_.@:-]{1,120}", host) and host not in seen:
            seen.add(host)
            hosts.append(host)
    return hosts


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize_model_name(value: Any, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if MODEL_NAME_RE.match(text):
        return text
    return default


def normalize_thread_id(item: dict[str, Any]) -> str:
    return str(item.get("threadId") or item.get("id") or item.get("thread_id") or "")


def title_without_emoji(title: str) -> str:
    text = title.strip()
    for emoji in sorted(KNOWN_EMOJI, key=len, reverse=True):
        if text.startswith(emoji):
            return text[len(emoji) :].strip()
    match = EMOJI_PREFIX_RE.match(text)
    if match:
        return text[match.end() :].strip()
    return text


def has_emoji_prefix(title: str) -> bool:
    text = title.strip()
    if any(text.startswith(f"{emoji} ") for emoji in sorted(KNOWN_EMOJI, key=len, reverse=True)):
        return True
    return EMOJI_PREFIX_RE.match(text) is not None


def session_fingerprint(session: dict[str, Any]) -> str:
    title = str(session.get("title") or session.get("thread_name") or "")
    preview = str(session.get("preview") or "")
    cwd = str(session.get("cwd") or "")
    context = str(session.get("contextSnippet") or "")
    raw = "\n".join(
        [
            normalize_thread_id(session),
            str(session.get("host") or "local"),
            title,
            sha_text(preview),
            sha_text(cwd),
            sha_text(context),
        ]
    )
    return sha_text(raw)


def cache_key(session: dict[str, Any], backend: str) -> str:
    title = str(session.get("title") or "")
    preview = str(session.get("preview") or "")
    cwd = str(session.get("cwd") or "")
    context = str(session.get("contextSnippet") or "")
    raw = "\n".join(
        [
            normalize_thread_id(session),
            str(session.get("host") or "local"),
            title,
            sha_text(preview),
            sha_text(cwd),
            sha_text(context),
            PROPOSER_VERSION,
            backend,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_time(value: Any) -> dt.datetime | None:
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), dt.timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_cache(path: Path) -> dict[str, Any]:
    data = read_json(path, {"version": 1, "entries": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "entries": {}}
    if not isinstance(data.get("entries"), dict):
        data["entries"] = {}
    data.setdefault("version", 1)
    return data


def prune_cache(cache_data: dict[str, Any], days: int) -> int:
    cutoff = utc_now() - dt.timedelta(days=days)
    entries = cache_data.get("entries", {})
    removed = 0
    for key in list(entries.keys()):
        created = parse_time(entries[key].get("created_at"))
        if created is None or created < cutoff:
            del entries[key]
            removed += 1
    return removed


def zip_dir(src: Path, dest: Path) -> int:
    files = [p for p in src.rglob("*") if p.is_file()]
    if not files:
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(files):
            rel = file_path.relative_to(src)
            zf.write(file_path, rel.as_posix())
    return len(files)


def clean_current(current: Path, tmp_root: Path) -> int:
    current = assert_under(current, tmp_root)
    if not current.exists():
        current.mkdir(parents=True, exist_ok=True)
        return 0
    removed = 0
    for child in current.iterdir():
        assert_under(child, tmp_root)
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
        removed += 1
    current.mkdir(parents=True, exist_ok=True)
    return removed


def run_maintenance(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)

    current = assert_under(p["current"], p["tmp_root"])
    backup_file = None
    backup_file_count = 0
    if current.exists() and any(current.iterdir()):
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        backup_file = p["backups"] / f"current-{stamp}.zip"
        backup_file_count = zip_dir(current, backup_file)

    removed_current_items = clean_current(current, p["tmp_root"])

    cache_data = load_cache(p["cache"])
    removed_cache_entries = prune_cache(cache_data, args.cache_days)
    write_json(p["cache"], cache_data)

    backups = sorted(p["backups"].glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    pruned_backups = []
    for old in backups[args.keep_backups :]:
        old.unlink()
        pruned_backups.append(str(old))

    report = {
        "created_at": iso_now(),
        "codex_home": str(home),
        "current_dir": str(current),
        "backup_file": str(backup_file) if backup_file else None,
        "backup_file_count": backup_file_count,
        "removed_current_items": removed_current_items,
        "removed_cache_entries": removed_cache_entries,
        "pruned_backups": pruned_backups,
        "retention": {"cache_days": args.cache_days, "keep_backups": args.keep_backups},
        "safety": "Only session-renamer cache/tmp paths were modified.",
    }
    write_json(current / "maintenance_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def extract_threads(data: Any, host: str, source: str) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("threads"), list):
        raw_items = data["threads"]
    elif isinstance(data, list):
        raw_items = data
    else:
        return []
    sessions = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        thread_id = normalize_thread_id(item)
        title = str(item.get("title") or item.get("thread_name") or "").strip()
        if not thread_id or not title:
            continue
        sessions.append(
            {
                "threadId": thread_id,
                "host": str(item.get("host") or host),
                "title": title,
                "preview": str(item.get("preview") or ""),
                "cwd": str(item.get("cwd") or ""),
                "updatedAt": item.get("updatedAt") or item.get("updated_at"),
                "status": item.get("status"),
                "source": source,
                "fingerprint": session_fingerprint(
                    {
                        "threadId": thread_id,
                        "host": str(item.get("host") or host),
                        "title": title,
                        "preview": str(item.get("preview") or ""),
                        "cwd": str(item.get("cwd") or ""),
                    }
                ),
            }
        )
    return sessions


def parse_session_index(path: Path, host: str) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            thread_id = normalize_thread_id(item)
            title = str(item.get("thread_name") or item.get("title") or "").strip()
            if not thread_id or not title:
                continue
            prev = latest.get(thread_id)
            if prev:
                prev_time = parse_time(prev.get("updatedAt"))
                new_time = parse_time(item.get("updated_at"))
                if prev_time and new_time and new_time <= prev_time:
                    continue
            latest[thread_id] = {
                "threadId": thread_id,
                "host": host,
                "title": title,
                "preview": "",
                "cwd": "",
                "updatedAt": item.get("updated_at"),
                "source": "session_index",
            }
    sessions = []
    for session in latest.values():
        session["fingerprint"] = session_fingerprint(session)
        sessions.append(session)
    return sessions


def fetch_remote_index(host: str, out_dir: Path) -> Path | None:
    safe_host = re.sub(r"[^A-Za-z0-9_.-]+", "_", host)
    out_path = out_dir / f"{safe_host}-session_index.jsonl"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        "cat ~/.codex/session_index.jsonl 2>/dev/null",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0 or not result.stdout.strip():
        return None
    out_path.write_text(result.stdout, encoding="utf-8", newline="\n")
    return out_path


def clean_snippet(text: str, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key|密码)\s*[:=]\s*\S+", r"\1: [redacted]", text)
    text = re.sub(r"(?i)(sudo\s+password|sudo\s+密码|密码(?:告诉你)?(?:是|为)?)\s*\S+", r"\1 [redacted]", text)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[redacted]", text)
    text = re.sub(r"(?i)\bli[0-9][A-Za-z0-9._-]{4,}\b", "[redacted]", text)
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def is_relevant_user_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped in ("<image>", "</image>"):
        return False
    noisy_prefixes = (
        "<environment_context>",
        "<app-context>",
        "<permissions instructions>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "# AGENTS.md instructions",
        "# In app browser:",
    )
    if stripped.startswith(noisy_prefixes):
        return False
    if "data:image/" in stripped and len(stripped) > 1000:
        return False
    return True


def normalize_user_text(text: str) -> str:
    marker = "## My request for Codex:"
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.strip()


def strings_from_content(content: Any) -> list[str]:
    chunks: list[str] = []
    if isinstance(content, str):
        chunks.append(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    chunks.append(item["text"])
                elif isinstance(item.get("input_text"), str):
                    chunks.append(item["input_text"])
            elif isinstance(item, str):
                chunks.append(item)
    return chunks


def extract_user_messages(transcript_path: Path, limit: int = 8) -> list[str]:
    messages: list[str] = []
    with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            text_parts: list[str] = []
            if payload.get("type") == "event_msg" and isinstance(payload.get("message"), str):
                text_parts.append(payload["message"])
            if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
                text_parts.append(payload["message"])
            if payload.get("type") == "message" and payload.get("role") == "user":
                text_parts.extend(strings_from_content(payload.get("content")))
            if payload.get("type") == "response_item":
                inner = payload.get("payload")
                if isinstance(inner, dict) and inner.get("type") == "message" and inner.get("role") == "user":
                    text_parts.extend(strings_from_content(inner.get("content")))
            for text in text_parts:
                text = normalize_user_text(text)
                if not is_relevant_user_text(text):
                    continue
                cleaned = clean_snippet(text)
                if cleaned and cleaned not in messages:
                    messages.append(cleaned)
                    if len(messages) >= limit:
                        return messages
    return messages


def extract_transcript_cwd(transcript_path: Path) -> str:
    with transcript_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("type") != "session_meta":
                continue
            payload = item.get("payload")
            if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                return payload["cwd"]
    return ""


def find_transcript(root: Path, thread_id: str) -> Path | None:
    if not root.exists():
        return None
    matches = list(root.rglob(f"*{thread_id}*.jsonl*"))
    if not matches:
        return None
    matches.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return matches[0]


def apply_context(session: dict[str, Any], messages: list[str], source: str) -> None:
    if not messages:
        return
    first = messages[0]
    latest = messages[-1]
    combined = first if first == latest else f"{first} | 最近: {latest}"
    session["firstUserMessage"] = first
    session["latestUserMessage"] = latest
    session["contextSnippet"] = clean_snippet(combined, 700)
    session["contextSource"] = source
    session["fingerprint"] = session_fingerprint(session)


def enrich_local_transcripts(sessions: list[dict[str, Any]], root: Path) -> int:
    count = 0
    for session in sessions:
        transcript = find_transcript(root, session["threadId"])
        if not transcript:
            continue
        if not session.get("cwd"):
            session["cwd"] = extract_transcript_cwd(transcript)
        messages = extract_user_messages(transcript)
        if messages:
            apply_context(session, messages, str(transcript))
            count += 1
    return count


def fetch_remote_contexts(host: str, thread_ids: list[str], timeout: int = 60) -> dict[str, dict[str, Any]]:
    if not thread_ids:
        return {}
    encoded = base64.b64encode(json.dumps(thread_ids).encode("utf-8")).decode("ascii")
    remote_script = r'''
import base64, json, pathlib, re, sys
ids = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
roots = [pathlib.Path.home() / ".codex" / "sessions", pathlib.Path.home() / ".codex" / "archived_sessions"]
def clean(text, limit=500):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"(?i)(password|passwd|token|secret|api[_-]?key|密码)\s*[:=]\s*\S+", r"\1: [redacted]", text)
    if len(text) > limit:
        return text[:limit-1].rstrip() + "…"
    return text
def relevant(text):
    text = text.strip()
    if not text:
        return False
    if text in ("<image>", "</image>"):
        return False
    prefixes = ("<environment_context>", "<app-context>", "<permissions instructions>", "<skills_instructions>", "<plugins_instructions>", "# AGENTS.md instructions", "# In app browser:")
    if text.startswith(prefixes):
        return False
    if "data:image/" in text and len(text) > 1000:
        return False
    return True
def normalize(text):
    marker = "## My request for Codex:"
    if marker in text:
        text = text.split(marker, 1)[1]
    return text.strip()
def strings(content):
    out = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    out.append(part["text"])
                elif isinstance(part.get("input_text"), str):
                    out.append(part["input_text"])
            elif isinstance(part, str):
                out.append(part)
    return out
def messages(path):
    out = []
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            try:
                item = json.loads(line)
            except Exception:
                continue
            payload = item.get("payload") if isinstance(item, dict) else None
            if not isinstance(payload, dict):
                continue
            parts = []
            if payload.get("type") == "event_msg" and isinstance(payload.get("message"), str):
                parts.append(payload["message"])
            if payload.get("type") == "user_message" and isinstance(payload.get("message"), str):
                parts.append(payload["message"])
            if payload.get("type") == "message" and payload.get("role") == "user":
                parts += strings(payload.get("content"))
            if payload.get("type") == "response_item":
                inner = payload.get("payload")
                if isinstance(inner, dict) and inner.get("type") == "message" and inner.get("role") == "user":
                    parts += strings(inner.get("content"))
            for part in parts:
                part = normalize(part)
                if not relevant(part):
                    continue
                part = clean(part)
                if part and part not in out:
                    out.append(part)
                    if len(out) >= 8:
                        return out
    return out
def cwd(path):
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return ""
    with fh:
        for line in fh:
            try:
                item = json.loads(line)
            except Exception:
                continue
            payload = item.get("payload") if isinstance(item, dict) else None
            if item.get("type") == "session_meta" and isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
                return payload["cwd"]
    return ""
result = {}
for tid in ids:
    found = []
    for root in roots:
        if root.exists():
            found += list(root.rglob("*" + tid + "*.jsonl*"))
    if not found:
        continue
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    result[tid] = {"messages": messages(found[0]), "cwd": cwd(found[0])}
print(json.dumps(result, ensure_ascii=False))
'''
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-", encoded]
    result = subprocess.run(
        cmd,
        input=remote_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            normalized[str(key)] = {"messages": [str(x) for x in value if isinstance(x, str)], "cwd": ""}
        elif isinstance(value, dict):
            messages = value.get("messages") if isinstance(value.get("messages"), list) else []
            normalized[str(key)] = {
                "messages": [str(x) for x in messages if isinstance(x, str)],
                "cwd": str(value.get("cwd") or ""),
            }
    return normalized


def run_discover(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    sessions: list[dict[str, Any]] = []

    if args.local_json:
        sessions.extend(extract_threads(read_json(Path(args.local_json), {}), "local", "codex_app"))

    for remote in args.remote_index or []:
        if "=" not in remote:
            raise SystemExit(f"--remote-index must be HOST=PATH, got: {remote}")
        host, file_name = remote.split("=", 1)
        sessions.extend(parse_session_index(Path(file_name), host))

    for host in args.ssh_host or []:
        snapshot = fetch_remote_index(host, p["current"])
        if snapshot:
            sessions.extend(parse_session_index(snapshot, host))

    by_id_host: dict[tuple[str, str], dict[str, Any]] = {}
    for session in sessions:
        key = (session["threadId"], session["host"])
        prev = by_id_host.get(key)
        if prev:
            prev_time = parse_time(prev.get("updatedAt"))
            new_time = parse_time(session.get("updatedAt"))
            if prev_time and new_time and new_time <= prev_time:
                continue
        by_id_host[key] = session

    sessions_list = list(by_id_host.values())
    enriched_local = 0
    enriched_remote = 0
    if args.enrich_transcripts:
        local_roots = args.local_sessions_root or [str(home / "sessions"), str(home / "archived_sessions")]
        for root in local_roots:
            enriched_local += enrich_local_transcripts(
                [s for s in sessions_list if s.get("host") in ("local", "localhost")],
                Path(root),
            )
        for host in args.ssh_host or []:
            host_sessions = [s for s in sessions_list if s.get("host") == host]
            contexts = fetch_remote_contexts(host, [s["threadId"] for s in host_sessions])
            for session in host_sessions:
                context = contexts.get(session["threadId"]) or {}
                if context.get("cwd") and not session.get("cwd"):
                    session["cwd"] = context["cwd"]
                messages = context.get("messages") or []
                if messages:
                    apply_context(session, messages, f"ssh:{host}:~/.codex/sessions")
                    enriched_remote += 1

    result = {
        "created_at": iso_now(),
        "count": len(sessions_list),
        "enriched_local": enriched_local,
        "enriched_remote": enriched_remote,
        "sessions": sorted(sessions_list, key=lambda x: (x.get("host") or "", x.get("title") or "")),
    }
    output = Path(args.output) if args.output else p["current"] / "sessions.json"
    write_json(output, result)
    print(json.dumps({"output": str(output), "count": result["count"]}, ensure_ascii=False, indent=2))
    return 0


def choose_emoji(title: str, preview: str = "", cwd: str = "") -> str:
    text = " ".join([title, preview, cwd]).lower()
    for needle, emoji in EMOJI_HINTS:
        if needle.lower() in text:
            return emoji
    return "🏷️"


def proposal_context(session: dict[str, Any]) -> dict[str, str]:
    cwd = str(session.get("cwd") or "")
    return {
        "preview": clean_snippet(str(session.get("preview") or ""), 500),
        "cwd": clean_snippet(cwd, 240) if cwd else "",
        "cwd_basename": clean_snippet(Path(cwd).name, 120) if cwd else "",
        "context": clean_snippet(str(session.get("contextSnippet") or ""), 900),
    }


def heuristic_proposal(session: dict[str, Any]) -> dict[str, Any]:
    old_title = str(session.get("title") or "")
    core = title_without_emoji(old_title)
    emoji = choose_emoji(core, str(session.get("preview") or ""), str(session.get("cwd") or ""))
    return {
        "threadId": session["threadId"],
        "host": session.get("host") or "local",
        "oldTitle": old_title,
        "newTitle": f"{emoji} {core}",
        "reason": "Generated by local heuristic from title keywords.",
        "backend": "heuristic",
        "fingerprint": session.get("fingerprint") or session_fingerprint(session),
        "context": proposal_context(session),
        "status": "proposed",
    }


def already_named_proposal(session: dict[str, Any]) -> dict[str, Any]:
    old_title = str(session.get("title") or "")
    return {
        "threadId": session["threadId"],
        "host": session.get("host") or "local",
        "oldTitle": old_title,
        "newTitle": old_title,
        "reason": "Skipped model proposal because the title already starts with an emoji.",
        "backend": "already_named",
        "fingerprint": session.get("fingerprint") or session_fingerprint(session),
        "context": proposal_context(session),
        "status": "already_named",
    }


def request_json(url: str, payload: dict[str, Any], api_key: str, timeout: int) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def normalize_openai_base_url(value: Any, default: str = DEFAULT_VLLM_BASE_URL) -> str:
    text = str(value or "").strip() or default
    if not URL_SCHEME_RE.match(text):
        text = f"http://{text}"
    parsed = urllib.parse.urlsplit(text)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) >= 2 and path_parts[-2:] == ["chat", "completions"]:
        path_parts = path_parts[:-2]
    while path_parts and path_parts[-1] in {"models", "completions"}:
        path_parts.pop()
    if not path_parts or path_parts[-1] != "v1":
        path_parts.append("v1")
    path = "/" + "/".join(path_parts)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def get_vllm_model(base_url: str, api_key: str, timeout: int) -> str:
    base_url = normalize_openai_base_url(base_url)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        method="GET",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    models = data.get("data") or []
    if not models:
        raise RuntimeError("No models returned by vLLM /v1/models")
    return str(models[0]["id"])


def vllm_proposals(
    sessions: list[dict[str, Any]],
    base_url: str,
    api_key: str,
    model: str | None,
    timeout: int,
    options: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    base_url = normalize_openai_base_url(base_url)
    if not model:
        model = get_vllm_model(base_url, api_key, timeout)
    compact_sessions = [
        {
            "id": s["threadId"],
            "host": s.get("host") or "local",
            "title": s.get("title") or "",
            "cwd_basename": clean_snippet(Path(str(s.get("cwd") or "")).name, 120),
            "preview": clean_snippet(str(s.get("preview") or ""), 160),
            "context": clean_snippet(str(s.get("contextSnippet") or ""), 1200),
        }
        for s in sessions
    ]
    option_text = ""
    if options:
        length = str(options.get("length") or "standard")
        style = str(options.get("style") or "descriptive")
        custom_size = str(options.get("custom_size") or "").strip()
        instruction = clean_snippet(str(options.get("instruction") or ""), 220)
        option_text = (
            f" User review options: length={length}; style={style}; "
            f"custom_size={custom_size or 'none'}; extra_instruction={instruction or 'none'}."
        )
    prompt = (
        "Generate content-aware Codex session title labels. Return only JSON with shape "
        '{"renames":[{"id":"...","new_title":"emoji + space + concise descriptive title","reason":"..."}]}. '
        "Rules: add exactly one leading emoji; make vague titles like 查看项目/查看项目进度/整理需求 become specific enough "
        "to identify the session content at a glance; preserve important project names, product names, and language; "
        "prefer Chinese if the original/context is Chinese; keep the title after emoji about 6-20 CJK chars or 2-6 English words; "
        "do not include secrets, IP passwords, or long paths; do not invent ids; keep reasons short."
        + option_text
        + " sessions="
        + json.dumps(compact_sessions, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Return only valid compact JSON. No markdown."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": max(260, min(1800, 300 * len(compact_sessions))),
        "response_format": {"type": "json_object"},
    }
    response = request_json(f"{base_url.rstrip('/')}/chat/completions", payload, api_key, timeout)
    content = response["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    by_id = {s["threadId"]: s for s in sessions}
    proposals = []
    for item in parsed.get("renames", []):
        thread_id = str(item.get("id") or item.get("threadId") or "")
        session = by_id.get(thread_id)
        if not session:
            continue
        proposals.append(
            {
                "threadId": thread_id,
                "host": session.get("host") or "local",
                "oldTitle": session.get("title") or "",
                "newTitle": str(item.get("new_title") or item.get("newTitle") or "").strip(),
                "reason": str(item.get("reason") or "Generated by local vLLM.").strip()[:240],
                "backend": "vllm",
                "model": model,
                "fingerprint": session.get("fingerprint") or session_fingerprint(session),
                "context": proposal_context(session),
                "status": "proposed",
            }
        )
    return proposals


def batched(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [items[index : index + size] for index in range(0, len(items), size)]


def validate_new_title(old_title: str, new_title: str) -> tuple[bool, str]:
    text = new_title.strip()
    if not has_emoji_prefix(text):
        return False, "new title must start with one emoji and a space"
    core_old = title_without_emoji(old_title)
    core_new = title_without_emoji(text)
    if not core_new:
        return False, "new title core is empty"
    if len(core_new) > 64:
        return False, "new title core is too long"
    if re.search(r"https?://|[A-Za-z]:\\|/home/|/Users/", core_new):
        return False, "new title must not include URLs or long paths"
    if re.search(r"(?i)(password|passwd|token|secret|api[_-]?key|密码)", core_new):
        return False, "new title must not include secret-like words"
    if core_new == core_old:
        return True, "ok"
    generic_titles = {
        "查看项目",
        "查看项目进度",
        "查看需求",
        "阅读项目文档",
        "整理备份需求信息",
        "排查异常问题",
        "查看项目用途",
        "查明错误原因",
        "回复问候",
        "问候",
        "接手对话会话",
        "查看session名称能否修改",
    }
    generic_terms = (
        "查看",
        "整理",
        "检查",
        "排查",
        "优化",
        "需求",
        "问题",
        "原因",
        "进度",
        "提权",
        "问候",
        "字体",
        "添加",
        "审查",
        "找出",
        "emoji",
        "create",
        "inspect",
        "investigate",
        "explain",
        "review",
        "architecture",
    )
    if core_old in generic_titles or any(term in core_old.lower() for term in generic_terms):
        if 2 <= len(core_new) <= 48:
            return True, "ok"
    token_re = r"[a-z0-9]+|[\u4e00-\u9fff]+"
    old_tokens = set(re.findall(token_re, core_old.lower()))
    new_tokens = set(re.findall(token_re, core_new.lower()))
    if old_tokens:
        overlap = len(old_tokens & new_tokens) / len(old_tokens)
        if overlap >= 0.35:
            return True, "ok"
    old_cjk = set(re.findall(r"[\u4e00-\u9fff]", core_old))
    new_cjk = set(re.findall(r"[\u4e00-\u9fff]", core_new))
    if old_cjk:
        overlap = len(old_cjk & new_cjk) / len(old_cjk)
        if overlap >= 0.4:
            return True, "ok"
    return False, "new title changes the original title too much"


def normalize_external_proposals(path: Path, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    data = read_json(path, {})
    items = data.get("renames") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise ValueError("subagent JSON must be a list or an object with renames")
    by_id = {s["threadId"]: s for s in sessions}
    proposals = []
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_id = str(item.get("id") or item.get("threadId") or "")
        session = by_id.get(thread_id)
        if not session:
            continue
        proposals.append(
            {
                "threadId": thread_id,
                "host": session.get("host") or "local",
                "oldTitle": session.get("title") or "",
                "newTitle": str(item.get("new_title") or item.get("newTitle") or "").strip(),
                "reason": str(item.get("reason") or "Generated by subagent.").strip()[:240],
                "backend": "subagent",
                "fingerprint": session.get("fingerprint") or session_fingerprint(session),
                "context": proposal_context(session),
                "status": "proposed",
            }
        )
    return proposals


def build_subagent_prompt(sessions: list[dict[str, Any]], context_limit: int = 900) -> str:
    compact = [
        {
            "id": s["threadId"],
            "host": s.get("host") or "local",
            "title": s.get("title") or "",
            "cwd_basename": clean_snippet(Path(str(s.get("cwd") or "")).name, 120),
            "preview": clean_snippet(str(s.get("preview") or ""), 160),
            "context": clean_snippet(str(s.get("contextSnippet") or ""), context_limit),
        }
        for s in sessions
    ]
    return (
        "Generate content-aware Codex session title labels. Return only JSON with this shape: "
        '{"renames":[{"id":"...","new_title":"emoji + space + concise descriptive title","reason":"..."}]}. '
        "Use the context to make vague titles specific enough to identify at a glance; preserve important project names; "
        "prefer Chinese if context is Chinese; avoid secrets; do not invent ids. sessions="
        + json.dumps(compact, ensure_ascii=False)
    )


def write_subagent_handoff(current: Path, sessions: list[dict[str, Any]], chunk_size: int = DEFAULT_SUBAGENT_CHUNK_SIZE) -> dict[str, Any]:
    chunk_size = max(1, int(chunk_size or DEFAULT_SUBAGENT_CHUNK_SIZE))
    prompt_dir = current / "subagent_prompts"
    result_dir = current / "subagent_results"
    for directory in (prompt_dir, result_dir):
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)

    chunks = batched(sessions, chunk_size)
    manifest_chunks = []
    for index, chunk in enumerate(chunks, start=1):
        stem = f"chunk-{index:03d}"
        prompt_path = prompt_dir / f"{stem}.prompt.txt"
        result_path = result_dir / f"{stem}.json"
        prompt_path.write_text(build_subagent_prompt(chunk), encoding="utf-8", newline="\n")
        manifest_chunks.append(
            {
                "index": index,
                "count": len(chunk),
                "ids": [s["threadId"] for s in chunk],
                "prompt_path": str(prompt_path),
                "result_path": str(result_path),
            }
        )

    manifest = {
        "created_at": iso_now(),
        "count": len(sessions),
        "chunk_size": chunk_size,
        "chunk_count": len(manifest_chunks),
        "prompt_dir": str(prompt_dir),
        "result_dir": str(result_dir),
        "output_path": str(current / "subagent_proposals.json"),
        "chunks": manifest_chunks,
    }
    write_json(current / "subagent_manifest.json", manifest)

    instruction_lines = [
        "Codex session rename subagent handoff.",
        "Goal: generate title proposals without applying any rename.",
        "",
        f"Read manifest: {current / 'subagent_manifest.json'}",
        f"Prompt chunks: {prompt_dir}",
        f"Write each JSON-only result into: {result_dir}",
        "",
        "For each chunk entry, spawn one Codex subagent using prompt_path and save the JSON-only reply to result_path.",
        'Each result must have shape: {"renames":[{"id":"...","new_title":"emoji + space + concise descriptive title","reason":"..."}]}',
        "Do not invent ids. If uncertain, omit that item; merge-subagent will report missing ids.",
        "",
        "After all chunks finish, run:",
        f"python {ps_quote(script_path_for_status())} merge-subagent --sessions {ps_quote(current / 'sessions.json')} --manifest {ps_quote(current / 'subagent_manifest.json')} --output {ps_quote(current / 'subagent_proposals.json')}",
        "",
        "Then rerun agent-review with --subagent-json subagent_proposals.json.",
    ]
    (current / "subagent_prompt.txt").write_text("\n".join(instruction_lines), encoding="utf-8", newline="\n")
    return manifest


def merge_subagent_result_files(
    sessions_path: Path,
    output_path: Path,
    *,
    manifest_path: Path | None = None,
    result_dir: Path | None = None,
    input_files: list[Path] | None = None,
) -> dict[str, Any]:
    sessions_data = read_json(sessions_path, {})
    sessions = sessions_data.get("sessions", []) if isinstance(sessions_data, dict) else sessions_data
    if not isinstance(sessions, list):
        raise ValueError("sessions input must be a list or object with sessions")
    valid_ids = {normalize_thread_id(s) for s in sessions if normalize_thread_id(s)}
    result_paths: list[Path] = []
    manifest = read_json(manifest_path, {}) if manifest_path and manifest_path.exists() else {}
    if isinstance(manifest, dict):
        for chunk in manifest.get("chunks", []):
            if isinstance(chunk, dict) and chunk.get("result_path"):
                result_paths.append(Path(str(chunk["result_path"])))
    if result_dir and result_dir.exists():
        result_paths.extend(sorted(result_dir.glob("*.json")))
    if input_files:
        result_paths.extend(input_files)

    seen_paths: set[Path] = set()
    renames_by_id: dict[str, dict[str, str]] = {}
    invalid_ids: list[str] = []
    parse_errors: list[dict[str, str]] = []
    duplicate_count = 0
    for path in result_paths:
        path = path.resolve()
        if path in seen_paths or not path.exists():
            continue
        seen_paths.add(path)
        data = read_json(path, None)
        items = data.get("renames") if isinstance(data, dict) else data
        if not isinstance(items, list):
            parse_errors.append({"path": str(path), "error": "missing renames list"})
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            thread_id = str(item.get("id") or item.get("threadId") or "")
            if thread_id not in valid_ids:
                invalid_ids.append(thread_id)
                continue
            if thread_id in renames_by_id:
                duplicate_count += 1
            renames_by_id[thread_id] = {
                "id": thread_id,
                "new_title": str(item.get("new_title") or item.get("newTitle") or "").strip(),
                "reason": str(item.get("reason") or "Generated by subagent.").strip()[:240],
            }

    ordered = [renames_by_id[normalize_thread_id(session)] for session in sessions if normalize_thread_id(session) in renames_by_id]
    missing_sessions = [session for session in sessions if normalize_thread_id(session) not in renames_by_id]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_json(output_path, {"renames": ordered})

    current = output_path.parent
    missing_path = current / "subagent_missing_sessions.json"
    missing_prompt_path = current / "subagent_missing_prompt.txt"
    if missing_sessions:
        write_json(missing_path, {"sessions": missing_sessions})
        missing_prompt_path.write_text(build_subagent_prompt(missing_sessions), encoding="utf-8", newline="\n")
    else:
        for stale in (missing_path, missing_prompt_path):
            if stale.exists():
                stale.unlink()

    report = {
        "created_at": iso_now(),
        "output_path": str(output_path),
        "sessions_count": len(sessions),
        "rename_count": len(ordered),
        "missing_count": len(missing_sessions),
        "missing_prompt_path": str(missing_prompt_path) if missing_sessions else None,
        "invalid_id_count": len(invalid_ids),
        "invalid_ids": invalid_ids[:50],
        "duplicate_count": duplicate_count,
        "result_files": [str(path) for path in sorted(seen_paths)],
        "parse_errors": parse_errors,
    }
    write_json(current / "subagent_merge_report.json", report)
    return report


def build_subagent_request(current: Path, body: dict[str, Any]) -> dict[str, Any]:
    raw_sessions = body.get("sessions") if isinstance(body, dict) else None
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError("missing sessions list")
    sessions: list[dict[str, Any]] = []
    for item in raw_sessions:
        if not isinstance(item, dict):
            continue
        context = item.get("context") if isinstance(item.get("context"), dict) else {}
        thread_id = str(item.get("threadId") or item.get("id") or "")
        old_title = str(item.get("oldTitle") or item.get("title") or "")
        if not thread_id or not old_title:
            continue
        sessions.append(
            {
                "threadId": thread_id,
                "host": str(item.get("host") or "local"),
                "title": old_title,
                "cwd": str(context.get("cwd") or item.get("cwd") or ""),
                "preview": str(context.get("preview") or item.get("preview") or ""),
                "contextSnippet": str(context.get("context") or item.get("contextSnippet") or ""),
                "fingerprint": item.get("fingerprint"),
            }
        )
    if not sessions:
        raise ValueError("no usable sessions in request")

    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    backend_config = body.get("backendConfig") if isinstance(body.get("backendConfig"), dict) else {}
    if backend_config.get("backend") and str(backend_config.get("backend")) != "codex":
        raise ValueError("subagent request requires Codex backend")
    preferred_model = normalize_model_name(
        backend_config.get("codex_model") or options.get("codex_model"),
        "gpt-5.3-codex-spark",
    )
    fallback_model = normalize_model_name(
        backend_config.get("codex_fallback_model") or options.get("codex_fallback_model"),
        "gpt-5.4-mini",
    )
    prompt = build_subagent_prompt(sessions)
    if options:
        prompt += "\n\nReview options: " + json.dumps(options, ensure_ascii=False)

    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    digest = sha_text(json.dumps([s["threadId"] for s in sessions], ensure_ascii=False))
    request_id = f"{stamp}-{digest}"
    request_dir = current / "subagent_requests"
    result_dir = current / "subagent_results"
    request_dir.mkdir(parents=True, exist_ok=True)
    result_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = request_dir / f"{request_id}.prompt.txt"
    request_path = request_dir / f"{request_id}.json"
    result_path = result_dir / f"{request_id}.result.json"
    prompt_path.write_text(prompt, encoding="utf-8", newline="\n")
    request = {
        "created_at": iso_now(),
        "request_id": request_id,
        "status": "pending_codex_subagent",
        "preferred_model": preferred_model,
        "fallback_model": fallback_model,
        "count": len(sessions),
        "sessions": sessions,
        "options": options,
        "backend_config": {
            "kind": "codex_subagent",
            "preferred_model": preferred_model,
            "fallback_model": fallback_model,
        },
        "prompt_path": str(prompt_path),
        "result_path": str(result_path),
        "agent_instructions": [
            f"Spawn a Codex subagent using {preferred_model} when available; fall back to {fallback_model}.",
            "If both requested models are unavailable, use the closest Codex-supported coding model and record the actual model used.",
            "Send the prompt from prompt_path.",
            "Require JSON only: {\"renames\":[{\"id\":\"...\",\"new_title\":\"emoji + title\",\"reason\":\"...\"}]}",
            "Save the validated JSON to result_path.",
        ],
    }
    write_json(request_path, request)
    write_json(
        current / "subagent_status.json",
        {
            "created_at": iso_now(),
            "state": "pending_codex_subagent",
            "request_id": request_id,
            "request_path": str(request_path),
            "prompt_path": str(prompt_path),
            "result_path": str(result_path),
            "count": len(sessions),
        },
    )
    return request


def subagent_request_status(current: Path, request_id: str | None = None) -> dict[str, Any]:
    status_path = current / "subagent_status.json"
    status = read_json(status_path, {}) if status_path.exists() else {}
    if not request_id and isinstance(status, dict):
        request_id = str(status.get("request_id") or "")
    if not request_id:
        return {"ok": True, "state": "none", "message": "No subagent request has been prepared."}
    result_path = current / "subagent_results" / f"{request_id}.result.json"
    if result_path.exists():
        result = read_json(result_path, {})
        renames = result.get("renames") if isinstance(result, dict) else []
        return {
            "ok": True,
            "state": "completed",
            "request_id": request_id,
            "result_path": str(result_path),
            "rename_count": len(renames) if isinstance(renames, list) else 0,
            "result": result,
        }
    if isinstance(status, dict) and status.get("request_id") == request_id:
        return {"ok": True, **status}
    return {"ok": True, "state": "pending_codex_subagent", "request_id": request_id, "result_path": str(result_path)}


def run_propose(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    input_path = Path(args.input) if args.input else p["current"] / "sessions.json"
    sessions_data = read_json(input_path, {})
    sessions = sessions_data.get("sessions", []) if isinstance(sessions_data, dict) else sessions_data
    if not isinstance(sessions, list):
        raise SystemExit("sessions input must be a list or object with sessions")

    cache_data = load_cache(p["cache"])
    proposals: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    cache_hits = 0
    skipped_existing_emoji = 0
    backend_key = args.backend
    include_existing_emoji = bool(getattr(args, "include_existing_emoji", False))
    force_refresh = args.backend == "subagent" and (bool(args.subagent_json) or bool(getattr(args, "force_refresh", False)))
    for session in sessions:
        if not normalize_thread_id(session):
            continue
        session["threadId"] = normalize_thread_id(session)
        session.setdefault("fingerprint", session_fingerprint(session))
        if not include_existing_emoji and has_emoji_prefix(str(session.get("title") or "")):
            proposals.append(already_named_proposal(session))
            skipped_existing_emoji += 1
            continue
        key = cache_key(session, backend_key)
        cached = cache_data["entries"].get(key)
        if cached and not force_refresh:
            proposal = dict(cached["proposal"])
            proposal["status"] = "cached"
            proposals.append(proposal)
            cache_hits += 1
        else:
            missing.append(session)

    backend_used = "cache"
    backend_error = None
    generated: list[dict[str, Any]] = []
    if missing:
        try:
            if args.backend == "subagent":
                if not args.subagent_json:
                    write_subagent_handoff(p["current"], missing, getattr(args, "subagent_chunk_size", DEFAULT_SUBAGENT_CHUNK_SIZE))
                    generated = [heuristic_proposal(s) | {"status": "subagent_needed"} for s in missing]
                    backend_used = "subagent_prompt"
                else:
                    generated = normalize_external_proposals(Path(args.subagent_json), missing)
                    generated_ids = {proposal.get("threadId") for proposal in generated}
                    for session in missing:
                        if session["threadId"] not in generated_ids:
                            generated.append(heuristic_proposal(session) | {"status": "fallback_missing_subagent"})
                    backend_used = "subagent"
            elif args.backend in ("vllm", "auto"):
                generated = []
                model = args.model
                if not model:
                    model = get_vllm_model(args.vllm_base_url, args.vllm_api_key, args.timeout)
                for chunk in batched(missing, args.batch_size):
                    generated.extend(vllm_proposals(chunk, args.vllm_base_url, args.vllm_api_key, model, args.timeout))
                backend_used = "vllm"
                by_id = {p["threadId"] for p in generated}
                for session in missing:
                    if session["threadId"] not in by_id:
                        generated.append(heuristic_proposal(session))
            elif args.backend == "heuristic":
                generated = [heuristic_proposal(s) for s in missing]
                backend_used = "heuristic"
            else:
                raise ValueError(f"Unknown backend: {args.backend}")
        except Exception as exc:  # noqa: BLE001
            backend_error = str(exc)
            if args.backend == "vllm":
                raise
            write_subagent_handoff(p["current"], missing, getattr(args, "subagent_chunk_size", DEFAULT_SUBAGENT_CHUNK_SIZE))
            generated = [heuristic_proposal(s) | {"status": "fallback_after_error"} for s in missing]
            backend_used = "heuristic_fallback"

    for proposal in generated:
        old = proposal.get("oldTitle") or ""
        new = proposal.get("newTitle") or ""
        valid, reason = validate_new_title(old, new)
        proposal["valid"] = valid
        proposal["validation"] = reason
        matching_session = next(
            (s for s in sessions if s.get("threadId") == proposal.get("threadId") and (s.get("host") or "local") == (proposal.get("host") or "local")),
            None,
        )
        if matching_session:
            key = cache_key(matching_session, backend_key)
            if proposal.get("status") not in {"fallback_missing_subagent", "fallback_after_error"}:
                cache_data["entries"][key] = {"created_at": iso_now(), "proposal": proposal}
        proposals.append(proposal)

    for proposal in proposals:
        old = proposal.get("oldTitle") or ""
        new = proposal.get("newTitle") or ""
        valid, reason = validate_new_title(old, new)
        proposal["valid"] = valid
        proposal["validation"] = reason

    write_json(p["cache"], cache_data)
    output = Path(args.output) if args.output else p["current"] / "proposals.json"
    result = {
        "created_at": iso_now(),
        "proposer_version": PROPOSER_VERSION,
        "backend_requested": args.backend,
        "backend_used": backend_used,
        "backend_error": backend_error,
        "cache_hits": cache_hits,
        "skipped_existing_emoji": skipped_existing_emoji,
        "model_input_count": len(missing),
        "count": len(proposals),
        "proposals": proposals,
    }
    write_json(output, result)
    print(
        json.dumps(
            {
                "output": str(output),
                "count": len(proposals),
                "backend_used": backend_used,
                "skipped_existing_emoji": skipped_existing_emoji,
                "model_input_count": len(missing),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def run_render_review(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    proposals_path = Path(args.input) if args.input else p["current"] / "proposals.json"
    data = read_json(proposals_path, {})
    maintenance = read_json(p["current"] / "maintenance_report.json", {})
    payload = {
        "created_at": iso_now(),
        "proposals_file": str(proposals_path),
        "approved_file_name": "approved.json",
        "current_dir": str(p["current"]),
        "paths": {
            "start_review_request": str(p["current"] / "start_review_request.json"),
            "subagent_prompt": str(p["current"] / "subagent_prompt.txt"),
            "subagent_manifest": str(p["current"] / "subagent_manifest.json"),
            "subagent_results": str(p["current"] / "subagent_results"),
            "agent_review_commands": str(p["current"] / "agent_review_commands.md"),
            "desktop_apply_request": str(p["current"] / "desktop_apply_request.json"),
            "desktop_apply_result": str(p["current"] / "desktop_apply_result.json"),
            "local_threads": str(p["current"] / "local_threads.json"),
            "review": str(p["current"] / "review.html"),
        },
        "maintenance": maintenance,
        "proposal_run": data,
        "remote_host_candidates": ssh_config_hosts(),
    }
    template_path = skill_dir() / "assets" / "review_template.html"
    template = template_path.read_text(encoding="utf-8")
    safe_payload = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    rendered = template.replace("__SESSION_RENAMER_DATA__", safe_payload)
    output = Path(args.output) if args.output else p["current"] / "review.html"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    print(json.dumps({"output": str(output), "count": data.get("count", 0)}, ensure_ascii=False, indent=2))
    return 0


def run_merge_subagent(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    current = p["current"]
    manifest_path = Path(args.manifest) if args.manifest else current / "subagent_manifest.json"
    sessions_path = Path(args.sessions) if args.sessions else current / "sessions.json"
    result_dir = Path(args.result_dir) if args.result_dir else current / "subagent_results"
    output_path = Path(args.output) if args.output else current / "subagent_proposals.json"
    input_files = [Path(path) for path in args.input or []]
    report = merge_subagent_result_files(
        sessions_path,
        output_path,
        manifest_path=manifest_path,
        result_dir=result_dir,
        input_files=input_files,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.strict and report["missing_count"]:
        return 2
    return 0


def run_bootstrap_review(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    current = p["current"]
    local_json = Path(args.local_json) if args.local_json else None
    staged_local = None
    if local_json and local_json.exists():
        stamp = utc_now().strftime("%Y%m%d-%H%M%S")
        staging = p["tmp_root"] / "bootstrap-staging" / stamp
        staging.mkdir(parents=True, exist_ok=True)
        staged_local = staging / "local_threads.json"
        shutil.copy2(local_json, staged_local)

    run_maintenance(
        argparse.Namespace(
            codex_home=str(home),
            cache_days=args.cache_days,
            keep_backups=args.keep_backups,
        )
    )

    current.mkdir(parents=True, exist_ok=True)
    if staged_local:
        shutil.copy2(staged_local, current / "local_threads.json")
    waiting = {
        "created_at": iso_now(),
        "proposer_version": PROPOSER_VERSION,
        "backend_requested": "user_config",
        "backend_used": "waiting_for_start",
        "backend_error": None,
        "cache_hits": 0,
        "count": 0,
        "proposals": [],
        "message": "Waiting for the user to configure a backend and click Start review.",
    }
    write_json(current / "proposals.json", waiting)
    write_json(
        current / "start_review_status.json",
        {
            "created_at": iso_now(),
            "state": "ready_for_user_config",
            "local_threads": str(current / "local_threads.json") if staged_local else None,
            "message": "Review page is ready. Configure the backend in the browser, then click Start review.",
        },
    )
    run_render_review(argparse.Namespace(codex_home=str(home), input=None, output=None))
    print(
        json.dumps(
            {
                "state": "ready_for_user_config",
                "review_path": str(current / "review.html"),
                "has_local_threads": bool(staged_local),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_apply_plan(approvals_path: Path, data: Any) -> dict[str, Any]:
    approvals = data.get("approvals") if isinstance(data, dict) else data
    if not isinstance(approvals, list):
        raise ValueError("approved JSON must be a list or an object with approvals")
    apply_items = []
    skipped = []
    for item in approvals:
        if not isinstance(item, dict):
            continue
        approved = item.get("approved") is True
        old_title = str(item.get("oldTitle") or "")
        new_title = str(item.get("newTitle") or "")
        valid, reason = validate_new_title(old_title, new_title)
        base = {
            "threadId": str(item.get("threadId") or ""),
            "host": str(item.get("host") or "local"),
            "oldTitle": old_title,
            "newTitle": new_title,
            "fingerprint": item.get("fingerprint"),
            "edited": bool(item.get("edited")),
        }
        if approved and base["threadId"] and valid:
            apply_items.append(base)
        else:
            base["skip_reason"] = "not approved" if not approved else reason
            skipped.append(base)
    return {
        "created_at": iso_now(),
        "source": str(approvals_path),
        "apply_count": len(apply_items),
        "skip_count": len(skipped),
        "apply": apply_items,
        "skipped": skipped,
        "instructions": "Codex must apply each item with codex_app.set_thread_title after revalidating current title/fingerprint.",
    }


def build_desktop_apply_request(apply_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    apply_items = plan.get("apply") if isinstance(plan, dict) else None
    if not isinstance(apply_items, list):
        raise ValueError("apply_plan.json is missing an apply list")

    items: list[dict[str, Any]] = []
    counts_by_host: dict[str, int] = {}
    for item in apply_items:
        if not isinstance(item, dict):
            continue
        thread_id = str(item.get("threadId") or "")
        new_title = str(item.get("newTitle") or "")
        if not thread_id or not new_title:
            continue
        host = str(item.get("host") or "local")
        counts_by_host[host] = counts_by_host.get(host, 0) + 1
        items.append(
            {
                "threadId": thread_id,
                "host": host,
                "oldTitle": str(item.get("oldTitle") or ""),
                "newTitle": new_title,
                "fingerprint": item.get("fingerprint"),
                "edited": bool(item.get("edited")),
                "status": "pending",
            }
        )

    return {
        "created_at": iso_now(),
        "source": str(apply_path),
        "status": "pending_codex_tool",
        "requires_tool": "codex_app.set_thread_title",
        "apply_count": len(items),
        "counts_by_host": counts_by_host,
        "items": items,
        "agent_command": "codex-session-emoji apply-approved",
        "runbook": "runbooks/apply-approved.md",
    }


def sanitized_start_backend_config(body: dict[str, Any]) -> dict[str, Any]:
    raw = body.get("backendConfig") if isinstance(body.get("backendConfig"), dict) else {}
    backend = str(raw.get("backend") or "openai")
    if backend not in {"openai", "codex"}:
        backend = "openai"
    config: dict[str, Any] = {"backend": backend}
    if backend == "openai":
        config.update(
            {
                "openai_base_url": normalize_openai_base_url(raw.get("openai_base_url") or DEFAULT_VLLM_BASE_URL),
                "openai_model": str(raw.get("openai_model") or "").strip(),
                "openai_api_key": "[not stored]",
                "api_key_note": "The review page does not persist API keys. Use local vLLM/default key or provide a key at execution time.",
            }
        )
    else:
        config.update(
            {
                "codex_model": normalize_model_name(raw.get("codex_model"), "gpt-5.3-codex-spark"),
                "codex_fallback_model": normalize_model_name(raw.get("codex_fallback_model"), "gpt-5.4-mini"),
            }
        )
    return config


def build_start_review_request(current: Path, body: dict[str, Any]) -> dict[str, Any]:
    backend_config = sanitized_start_backend_config(body if isinstance(body, dict) else {})
    remote_hosts = sanitize_remote_hosts(body.get("remoteHosts") if isinstance(body, dict) else [])
    request = {
        "created_at": iso_now(),
        "status": "pending_codex_agent",
        "request": "start_new_review",
        "backend_config": backend_config,
        "remote_hosts": remote_hosts,
        "output_dir": str(current),
        "expected_outputs": [
            "maintenance_report.json",
            "local_threads.json",
            "sessions.json",
            "proposals.json",
            "review.html",
        ],
        "agent_command": "codex-session-emoji start-review",
        "runbook": "runbooks/start-review-from-web.md",
    }
    return request


def write_start_review_request(current: Path, body: dict[str, Any]) -> dict[str, Any]:
    request = build_start_review_request(current, body)
    request_path = current / "start_review_request.json"
    write_json(request_path, request)
    status = {
        "created_at": iso_now(),
        "state": "pending_codex_agent",
        "request_path": str(request_path),
        "message": "Waiting for Codex agent to start a fresh review.",
    }
    write_json(current / "start_review_status.json", status)
    return {"request_path": request_path, "request": request, "status": status}


def skill_quickstart_text(language: str = "zh") -> str:
    script = Path(__file__).resolve()
    local_threads = paths()["current"] / "local_threads.json"
    if language.lower().startswith("en"):
        return "\n".join(
            [
                "Codex Session Rename Review works as review first, apply later.",
                "",
                "1. Back up and clean only this skill's previous generated files.",
                "2. Collect current Codex Desktop-visible sessions and open the local configuration page.",
                "3. After the user clicks Start review, build short redacted context snippets and generate title proposals.",
                "4. Refresh the local review page so the user can approve, reject, or edit titles.",
                "5. Apply only approved titles later through the official Codex thread title tool.",
                "",
                "Safety: opening the first page never generates or renames sessions; the web page cannot call Codex title tools directly; approved results are checked again before apply.",
                "",
                "First page after saving codex_app.list_threads output to local_threads.json:",
                f"python {ps_quote(script)} bootstrap-review --local-json {ps_quote(local_threads)}",
                "",
                "Scriptable generation fallback:",
                f"python {ps_quote(script)} agent-review --local-json {ps_quote(local_threads)}",
            ]
        )
    return "\n".join(
        [
            "Codex 会话重命名审核采用“先审核、后应用”的流程。",
            "",
            "1. 先备份并清理本 skill 上一次生成的临时文件，不碰全局会话数据。",
            "2. 先拉取当前 Codex Desktop 可见会话，并打开本地配置页。",
            "3. 用户点击“开始审核”后，再读取少量脱敏上下文并生成标题建议。",
            "4. 刷新本地审核页，用户可以批准、拒绝或手动编辑标题。",
            "5. 用户提交审核后，才通过 Codex 官方 thread title 工具应用已批准标题。",
            "",
            "安全边界：首次打开网页不会生成或改名；网页不能直接调用 Codex 改名工具；真正应用前会再次以批准结果为准。",
            "",
            "保存 codex_app.list_threads 输出到 local_threads.json 后，先打开配置页：",
            f"python {ps_quote(script)} bootstrap-review --local-json {ps_quote(local_threads)}",
            "",
            "需要脚本化生成时可用：",
            f"python {ps_quote(script)} agent-review --local-json {ps_quote(local_threads)}",
        ]
    )


def run_quickstart(args: argparse.Namespace) -> int:
    body = skill_quickstart_text(args.lang)
    if args.format == "json":
        print(json.dumps({"language": args.lang, "text": body}, ensure_ascii=False, indent=2))
    else:
        print(body)
    return 0


def backend_from_start_request(request: dict[str, Any], override: str | None = None) -> str:
    if override:
        return "subagent" if override == "codex" else override
    config = request.get("backend_config") if isinstance(request.get("backend_config"), dict) else {}
    selected = str(config.get("backend") or "auto").lower()
    if selected == "codex":
        return "subagent"
    if selected == "openai":
        return "vllm"
    if selected in {"auto", "vllm", "subagent", "heuristic"}:
        return selected
    return "auto"


def ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def write_agent_review_commands(
    current: Path,
    *,
    local_json: Path,
    request_path: Path,
    backend: str,
    subagent_json: Path | None,
    port: int,
    state: str,
) -> Path:
    script = Path(__file__).resolve()
    restart = script.parent / "restart_review_server.ps1"
    subagent_part = ""
    if backend == "subagent":
        subagent_part = f"""\

## Codex Subagent Handoff

If `agent_review_status.json` says `needs_subagent`, read `runbooks/codex-subagent.md` and use:

```text
prompt:   {current / "subagent_prompt.txt"}
manifest: {current / "subagent_manifest.json"}
output:   {current / "subagent_proposals.json"}
```

Finish after merge:

```powershell
python {ps_quote(script)} agent-review --local-json {ps_quote(local_json)} --request {ps_quote(request_path)} --backend subagent --subagent-json {ps_quote(current / "subagent_proposals.json")} --port {port}
```
"""
    content = f"""# Codex Session Rename Agent Commands

Generated: {iso_now()}
State: `{state}`
Backend: `{backend}`

Short path for a web-requested review cycle. For full details, read `runbooks/start-review-from-web.md`.

## Desktop Thread Snapshot

Call `codex_app.list_threads` and save the tool result as:

```text
{local_json}
```

## One Command For Scriptable Steps

```powershell
python {ps_quote(script)} agent-review --local-json {ps_quote(local_json)} --request {ps_quote(request_path)} --backend {backend} --port {port}
```
{subagent_part}

## Restart Review Server

```powershell
powershell -ExecutionPolicy Bypass -File {ps_quote(restart)} -Port {port}
```

## Safety Boundary

This only prepares `review.html`. Applying approved titles is a separate `desktop_apply_request.json` step.
"""
    output = current / "agent_review_commands.md"
    output.write_text(content, encoding="utf-8", newline="\n")
    return output


def run_agent_review(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    current = p["current"]
    request_path = Path(args.request) if args.request else current / "start_review_request.json"
    local_json = Path(args.local_json) if args.local_json else current / "local_threads.json"
    if not local_json.exists():
        raise SystemExit(f"local thread snapshot not found: {local_json}")

    request = read_json(request_path, {}) if request_path.exists() else {}
    backend = backend_from_start_request(request if isinstance(request, dict) else {}, args.backend)
    if backend not in {"auto", "vllm", "subagent", "heuristic"}:
        raise SystemExit(f"unsupported backend: {backend}")

    stamp = utc_now().strftime("%Y%m%d-%H%M%S")
    staging = p["tmp_root"] / "agent-staging" / stamp
    staging.mkdir(parents=True, exist_ok=True)
    staged_local = staging / "local_threads.json"
    shutil.copy2(local_json, staged_local)
    staged_request = staging / "start_review_request.json"
    if request_path.exists():
        shutil.copy2(request_path, staged_request)
    staged_subagent_json = None
    if args.subagent_json:
        source = Path(args.subagent_json)
        if not source.exists():
            raise SystemExit(f"subagent JSON not found: {source}")
        staged_subagent_json = staging / "subagent_proposals.json"
        shutil.copy2(source, staged_subagent_json)

    run_maintenance(
        argparse.Namespace(
            codex_home=str(home),
            cache_days=args.cache_days,
            keep_backups=args.keep_backups,
        )
    )

    current.mkdir(parents=True, exist_ok=True)
    current_local = current / "local_threads.json"
    shutil.copy2(staged_local, current_local)
    current_request = current / "start_review_request.json"
    if staged_request.exists():
        shutil.copy2(staged_request, current_request)
    current_subagent_json = None
    if staged_subagent_json:
        current_subagent_json = current / "subagent_proposals.json"
        shutil.copy2(staged_subagent_json, current_subagent_json)

    request_remote_hosts = sanitize_remote_hosts(request.get("remote_hosts")) if isinstance(request, dict) else []
    ssh_hosts = list(args.ssh_host or request_remote_hosts)

    run_discover(
        argparse.Namespace(
            codex_home=str(home),
            local_json=str(current_local),
            remote_index=args.remote_index,
            ssh_host=ssh_hosts,
            output=None,
            enrich_transcripts=True,
            local_sessions_root=None,
        )
    )

    config = request.get("backend_config") if isinstance(request.get("backend_config"), dict) else {}
    vllm_base_url = normalize_openai_base_url(args.vllm_base_url or config.get("openai_base_url") or DEFAULT_VLLM_BASE_URL)
    vllm_model = args.model if args.model is not None else str(config.get("openai_model") or "") or None
    vllm_api_key = args.vllm_api_key or os.environ.get("SESSION_RENAMER_OPENAI_API_KEY") or DEFAULT_VLLM_API_KEY

    if backend == "subagent" and not current_subagent_json:
        run_propose(
            argparse.Namespace(
                codex_home=str(home),
                input=None,
                output=None,
                backend="subagent",
                vllm_base_url=vllm_base_url,
                vllm_api_key=vllm_api_key,
                model=vllm_model,
                timeout=args.timeout,
                batch_size=args.batch_size,
                subagent_json=None,
                force_refresh=True,
                subagent_chunk_size=getattr(args, "subagent_chunk_size", DEFAULT_SUBAGENT_CHUNK_SIZE),
                include_existing_emoji=getattr(args, "include_existing_emoji", False),
            )
        )
        proposals_run = read_json(current / "proposals.json", {})
        if isinstance(proposals_run, dict) and proposals_run.get("backend_used") == "cache":
            run_render_review(argparse.Namespace(codex_home=str(home), input=None, output=None))
            command_path = write_agent_review_commands(
                current,
                local_json=current_local,
                request_path=current_request,
                backend=backend,
                subagent_json=None,
                port=args.port,
                state="review_ready",
            )
            status = {
                "created_at": iso_now(),
                "state": "review_ready",
                "backend": backend,
                "review_path": str(current / "review.html"),
                "commands_path": str(command_path),
                "serve_command": f"powershell -ExecutionPolicy Bypass -File {script_dir_for_status() / 'restart_review_server.ps1'} -Port {args.port}",
                "message": "All Codex-backend proposals were served from cache. Review page generated without a new subagent call.",
            }
            write_json(current / "agent_review_status.json", status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0

        prompt_path = current / "subagent_prompt.txt"
        manifest = read_json(current / "subagent_manifest.json", {})
        command_path = write_agent_review_commands(
            current,
            local_json=current_local,
            request_path=current_request,
            backend=backend,
            subagent_json=None,
            port=args.port,
            state="needs_subagent",
        )
        status = {
            "created_at": iso_now(),
            "state": "needs_subagent",
            "backend": backend,
            "prompt_path": str(prompt_path),
            "manifest_path": str(current / "subagent_manifest.json"),
            "result_dir": str(current / "subagent_results"),
            "chunk_count": manifest.get("chunk_count") if isinstance(manifest, dict) else None,
            "commands_path": str(command_path),
            "next_command": (
                f"python {script_path_for_status()} agent-review --local-json {current_local} "
                f"--request {current_request} --backend subagent --subagent-json {current / 'subagent_proposals.json'}"
            ),
            "message": "Subagent backend selected. Run chunk prompts from subagent_manifest.json, merge with merge-subagent, then rerun agent-review with --subagent-json.",
        }
        write_json(current / "agent_review_status.json", status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0

    run_propose(
        argparse.Namespace(
            codex_home=str(home),
            input=None,
            output=None,
            backend=backend,
            vllm_base_url=vllm_base_url,
            vllm_api_key=vllm_api_key,
            model=vllm_model,
            timeout=args.timeout,
            batch_size=args.batch_size,
            subagent_json=str(current_subagent_json) if current_subagent_json else None,
            force_refresh=False,
            subagent_chunk_size=getattr(args, "subagent_chunk_size", DEFAULT_SUBAGENT_CHUNK_SIZE),
            include_existing_emoji=getattr(args, "include_existing_emoji", False),
        )
    )
    run_render_review(argparse.Namespace(codex_home=str(home), input=None, output=None))
    command_path = write_agent_review_commands(
        current,
        local_json=current_local,
        request_path=current_request,
        backend=backend,
        subagent_json=current_subagent_json,
        port=args.port,
        state="review_ready",
    )
    status = {
        "created_at": iso_now(),
        "state": "review_ready",
        "backend": backend,
        "review_path": str(current / "review.html"),
        "commands_path": str(command_path),
        "serve_command": f"powershell -ExecutionPolicy Bypass -File {script_dir_for_status() / 'restart_review_server.ps1'} -Port {args.port}",
        "message": "Review page generated. Restart or refresh the local review server.",
    }
    write_json(current / "agent_review_status.json", status)
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


def script_path_for_status() -> str:
    return str(Path(__file__).resolve())


def script_dir_for_status() -> Path:
    return Path(__file__).resolve().parent


def write_desktop_apply_status(current: Path, request: dict[str, Any]) -> dict[str, Any]:
    status = {
        "created_at": iso_now(),
        "state": "pending_codex_tool",
        "request_path": str(current / "desktop_apply_request.json"),
        "result_path": str(current / "desktop_apply_result.json"),
        "apply_count": request.get("apply_count", 0),
        "counts_by_host": request.get("counts_by_host", {}),
        "message": "Waiting for Codex Desktop tool execution.",
    }
    write_json(current / "desktop_apply_status.json", status)
    return status


def desktop_apply_status(current: Path) -> dict[str, Any]:
    result_path = current / "desktop_apply_result.json"
    status_path = current / "desktop_apply_status.json"
    request_path = current / "desktop_apply_request.json"
    if result_path.exists():
        result = read_json(result_path, {})
        applied = result.get("applied") if isinstance(result, dict) else []
        failed = result.get("failed") if isinstance(result, dict) else []
        skipped = result.get("skipped") if isinstance(result, dict) else []
        return {
            "ok": True,
            "state": "completed",
            "result_path": str(result_path),
            "applied_count": len(applied) if isinstance(applied, list) else 0,
            "failed_count": len(failed) if isinstance(failed, list) else 0,
            "skipped_count": len(skipped) if isinstance(skipped, list) else 0,
            "result": result,
        }
    if status_path.exists():
        status = read_json(status_path, {})
        if isinstance(status, dict):
            return {"ok": True, **status}
    if request_path.exists():
        request = read_json(request_path, {})
        return {
            "ok": True,
            "state": "pending_codex_tool",
            "request_path": str(request_path),
            "result_path": str(result_path),
            "apply_count": request.get("apply_count", 0) if isinstance(request, dict) else 0,
        }
    return {"ok": True, "state": "none", "message": "No desktop apply request has been prepared."}


def save_approvals_plan_and_desktop_request(current: Path, body: dict[str, Any]) -> dict[str, Any]:
    approvals = body.get("approvals") if isinstance(body, dict) else None
    if not isinstance(approvals, list):
        raise ValueError("missing approvals list")
    approved_path = current / "approved.json"
    apply_path = current / "apply_plan.json"
    desktop_request_path = current / "desktop_apply_request.json"
    write_json(approved_path, body)
    plan = build_apply_plan(approved_path, body)
    write_json(apply_path, plan)
    desktop_request = build_desktop_apply_request(apply_path, plan)
    write_json(desktop_request_path, desktop_request)
    desktop_status = write_desktop_apply_status(current, desktop_request)
    return {
        "approved_path": approved_path,
        "apply_path": apply_path,
        "plan": plan,
        "desktop_request_path": desktop_request_path,
        "desktop_request": desktop_request,
        "desktop_status": desktop_status,
    }


def run_serve_review(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    current = assert_under(p["current"], p["tmp_root"])
    index_file = current / "review.html"
    if not index_file.exists():
        raise SystemExit(f"review.html not found: {index_file}")

    model_cache: dict[str, str] = {}

    class ReviewHandler(http.server.BaseHTTPRequestHandler):
        server_version = "SessionRenamerReview/1.0"

        def log_message(self, format: str, *log_args: Any) -> None:  # noqa: A002
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % log_args))

        def send_json(self, status: int, body: dict[str, Any]) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            requested = urllib.parse.unquote(self.path.split("?", 1)[0])
            if requested == "/api/desktop-apply-status":
                self.send_json(200, desktop_apply_status(current))
                return
            if requested == "/api/subagent-status":
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                request_id = (query.get("request_id") or [""])[0] or None
                self.send_json(200, subagent_request_status(current, request_id))
                return
            if requested in ("", "/"):
                file_path = index_file
            else:
                rel = requested.lstrip("/").replace("/", os.sep)
                file_path = current / rel
            try:
                file_path = assert_under(file_path, current)
            except ValueError:
                self.send_error(403)
                return
            if not file_path.exists() or not file_path.is_file():
                self.send_error(404)
                return
            content = file_path.read_bytes()
            content_type = "text/html; charset=utf-8" if file_path.suffix.lower() == ".html" else "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:  # noqa: N802
            endpoint = self.path.split("?", 1)[0]
            if endpoint not in (
                "/api/regenerate",
                "/api/start-review-request",
                "/api/save-approvals",
                "/api/prepare-desktop-apply",
                "/api/request-subagent",
                "/api/apply-remote-index",
                "/api/apply-remote-app-server",
                "/api/apply-remote-state",
            ):
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_json(400, {"ok": False, "error": "invalid content length"})
                return
            if size <= 0 or size > 2_000_000:
                self.send_json(413, {"ok": False, "error": "request body too large or empty"})
                return
            try:
                body = json.loads(self.rfile.read(size).decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                self.send_json(400, {"ok": False, "error": f"invalid JSON: {exc}"})
                return
            if endpoint == "/api/start-review-request":
                try:
                    saved = write_start_review_request(current, body)
                    request = saved["request"]
                    local_json = current / "local_threads.json"
                    agent_status = saved["status"]
                    direct_run = False
                    selected_remote_hosts = sanitize_remote_hosts(body.get("remoteHosts") if isinstance(body, dict) else [])
                    if local_json.exists():
                        backend_config = body.get("backendConfig") if isinstance(body.get("backendConfig"), dict) else {}
                        run_agent_review(
                            argparse.Namespace(
                                codex_home=str(home),
                                request=str(saved["request_path"]),
                                local_json=str(local_json),
                                backend=None,
                                subagent_json=None,
                                remote_index=None,
                                ssh_host=selected_remote_hosts,
                                vllm_base_url=None,
                                vllm_api_key=str(backend_config.get("openai_api_key") or "") or None,
                                model=None,
                                timeout=args.timeout,
                                batch_size=1,
                                port=args.port,
                                cache_days=DEFAULT_RETENTION_DAYS,
                                keep_backups=DEFAULT_BACKUP_KEEP,
                                subagent_chunk_size=DEFAULT_SUBAGENT_CHUNK_SIZE,
                            )
                        )
                        agent_status = read_json(current / "agent_review_status.json", agent_status)
                        direct_run = True
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "request_path": str(saved["request_path"]),
                            "status": agent_status,
                            "state": agent_status.get("state"),
                            "direct_run": direct_run,
                            "backend_config": request["backend_config"],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/apply-remote-index":
                try:
                    apply_path = current / "apply_plan.json"
                    plan = read_json(apply_path, {})
                    apply_items = plan.get("apply") if isinstance(plan, dict) else None
                    if not isinstance(apply_items, list):
                        raise ValueError("apply_plan.json is missing an apply list")
                    selected_hosts = body.get("hosts") if isinstance(body.get("hosts"), list) else []
                    report = apply_index_items(
                        apply_path,
                        apply_items,
                        home,
                        p,
                        {str(host) for host in selected_hosts if str(host).strip()},
                        False,
                        args.timeout,
                    )
                    output = current / "apply_index_report.json"
                    write_json(output, report)
                    self.send_json(
                        200,
                        {
                            "ok": not report["failed"],
                            "report_path": str(output),
                            "applied_count": sum(item["count"] for item in report["applied"]),
                            "applied": report["applied"],
                            "failed": report["failed"],
                            "skipped_count": len(report["skipped"]),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/apply-remote-app-server":
                try:
                    apply_path = current / "apply_plan.json"
                    plan = read_json(apply_path, {})
                    apply_items = plan.get("apply") if isinstance(plan, dict) else None
                    if not isinstance(apply_items, list):
                        raise ValueError("apply_plan.json is missing an apply list")
                    selected_hosts = body.get("hosts") if isinstance(body.get("hosts"), list) else []
                    report = apply_app_server_items(
                        apply_path,
                        apply_items,
                        {str(host) for host in selected_hosts if str(host).strip()},
                        False,
                        args.timeout,
                    )
                    output = current / "apply_app_server_report.json"
                    write_json(output, report)
                    self.send_json(
                        200,
                        {
                            "ok": not report["failed"],
                            "report_path": str(output),
                            "applied_count": sum(item["count"] for item in report["applied"]),
                            "applied": report["applied"],
                            "failed": report["failed"],
                            "skipped_count": len(report["skipped"]),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/apply-remote-state":
                try:
                    apply_path = current / "apply_plan.json"
                    plan = read_json(apply_path, {})
                    apply_items = plan.get("apply") if isinstance(plan, dict) else None
                    if not isinstance(apply_items, list):
                        raise ValueError("apply_plan.json is missing an apply list")
                    selected_hosts = body.get("hosts") if isinstance(body.get("hosts"), list) else []
                    report = apply_state_cache_items(
                        apply_path,
                        apply_items,
                        {str(host) for host in selected_hosts if str(host).strip()},
                        False,
                        args.timeout,
                    )
                    output = current / "apply_state_report.json"
                    write_json(output, report)
                    self.send_json(
                        200,
                        {
                            "ok": not report["failed"],
                            "report_path": str(output),
                            "applied_count": sum(item["count"] for item in report["applied"]),
                            "applied": report["applied"],
                            "failed": report["failed"],
                            "skipped_count": len(report["skipped"]),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/save-approvals":
                try:
                    saved = save_approvals_plan_and_desktop_request(current, body)
                    plan = saved["plan"]
                    desktop_request = saved["desktop_request"]
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "approved_path": str(saved["approved_path"]),
                            "apply_plan_path": str(saved["apply_path"]),
                            "desktop_apply_request_path": str(saved["desktop_request_path"]),
                            "desktop_apply_result_path": str(current / "desktop_apply_result.json"),
                            "desktop_apply_count": desktop_request["apply_count"],
                            "desktop_counts_by_host": desktop_request["counts_by_host"],
                            "apply_count": plan["apply_count"],
                            "skip_count": plan["skip_count"],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/prepare-desktop-apply":
                try:
                    if isinstance(body, dict) and isinstance(body.get("approvals"), list):
                        saved = save_approvals_plan_and_desktop_request(current, body)
                        request = saved["desktop_request"]
                        request_path = saved["desktop_request_path"]
                    else:
                        apply_path = current / "apply_plan.json"
                        plan = read_json(apply_path, {})
                        request = build_desktop_apply_request(apply_path, plan)
                        request_path = current / "desktop_apply_request.json"
                        write_json(request_path, request)
                        write_desktop_apply_status(current, request)
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "desktop_apply_request_path": str(request_path),
                            "desktop_apply_result_path": str(current / "desktop_apply_result.json"),
                            "desktop_apply_count": request["apply_count"],
                            "desktop_counts_by_host": request["counts_by_host"],
                            "status": desktop_apply_status(current),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            if endpoint == "/api/request-subagent":
                try:
                    request = build_subagent_request(current, body)
                    self.send_json(
                        200,
                        {
                            "ok": True,
                            "request_id": request["request_id"],
                            "request_path": str(current / "subagent_requests" / f"{request['request_id']}.json"),
                            "prompt_path": request["prompt_path"],
                            "result_path": request["result_path"],
                            "count": request["count"],
                            "preferred_model": request["preferred_model"],
                            "fallback_model": request["fallback_model"],
                            "status": subagent_request_status(current, request["request_id"]),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    self.send_json(400, {"ok": False, "error": str(exc)})
                return
            session = body.get("session") if isinstance(body, dict) else None
            if not isinstance(session, dict):
                self.send_json(400, {"ok": False, "error": "missing session"})
                return
            context = session.get("context") if isinstance(session.get("context"), dict) else {}
            thread_id = str(session.get("threadId") or session.get("id") or "")
            old_title = str(session.get("oldTitle") or session.get("title") or "")
            if not thread_id or not old_title:
                self.send_json(400, {"ok": False, "error": "missing threadId or title"})
                return
            try:
                backend_config = body.get("backendConfig") if isinstance(body.get("backendConfig"), dict) else {}
                if str(backend_config.get("backend") or "openai") != "openai":
                    self.send_json(400, {"ok": False, "error": "regenerate endpoint requires OpenAI-compatible backend"})
                    return
                base_url = normalize_openai_base_url(backend_config.get("openai_base_url") or args.vllm_base_url)
                api_key = str(backend_config.get("openai_api_key") or args.vllm_api_key)
                model = str(backend_config.get("openai_model") or args.model or "").strip()
                cache_key_for_model = f"{base_url}|{sha_text(api_key) if api_key else 'no-key'}"
                if not model:
                    model = model_cache.get(cache_key_for_model) or ""
                if not model:
                    model = get_vllm_model(base_url, api_key, args.timeout)
                    model_cache[cache_key_for_model] = model
                session_for_model = {
                    "threadId": thread_id,
                    "host": str(session.get("host") or "local"),
                    "title": old_title,
                    "preview": str(context.get("preview") or session.get("preview") or ""),
                    "cwd": str(context.get("cwd") or session.get("cwd") or ""),
                    "contextSnippet": str(context.get("context") or session.get("contextSnippet") or ""),
                    "fingerprint": session.get("fingerprint"),
                }
                generated = vllm_proposals(
                    [session_for_model],
                    base_url,
                    api_key,
                    model,
                    args.timeout,
                    body.get("options") if isinstance(body.get("options"), dict) else None,
                )
                if not generated:
                    raise RuntimeError("vLLM returned no proposal")
                proposal = generated[0]
                valid, reason = validate_new_title(proposal.get("oldTitle") or "", proposal.get("newTitle") or "")
                proposal["valid"] = valid
                proposal["validation"] = reason
                proposal["status"] = "regenerated"
                self.send_json(200, {"ok": True, "proposal": proposal})
            except Exception as exc:  # noqa: BLE001
                self.send_json(500, {"ok": False, "error": str(exc)})

    server = http.server.ThreadingHTTPServer((args.host, args.port), ReviewHandler)
    print(json.dumps({"url": f"http://{args.host}:{args.port}/", "current": str(current)}, ensure_ascii=False, indent=2))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def run_prepare_apply(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    approvals_path = Path(args.approvals) if args.approvals else p["current"] / "approved.json"
    data = read_json(approvals_path, {})
    output = Path(args.output) if args.output else p["current"] / "apply_plan.json"
    try:
        result = build_apply_plan(approvals_path, data)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    write_json(output, result)
    print(json.dumps({"output": str(output), "apply_count": result["apply_count"], "skip_count": result["skip_count"]}, ensure_ascii=False, indent=2))
    return 0


def index_jsonl_line(item: dict[str, Any], updated_at: str) -> str:
    return json.dumps(
        {
            "id": str(item["threadId"]),
            "thread_name": str(item["newTitle"]),
            "updated_at": updated_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_index_latest(text: str) -> dict[str, str]:
    latest: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        thread_id = normalize_thread_id(item)
        title = str(item.get("thread_name") or item.get("title") or "").strip()
        if thread_id and title:
            latest[thread_id] = title
    return latest


def local_index_latest(home: Path) -> dict[str, str]:
    index = home / "session_index.jsonl"
    if not index.exists():
        raise FileNotFoundError(f"local session_index.jsonl not found: {index}")
    return parse_index_latest(index.read_text(encoding="utf-8", errors="replace"))


def remote_index_latest(host: str, timeout: int) -> dict[str, str]:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
        "cat ~/.codex/session_index.jsonl 2>/dev/null",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ssh exited {result.returncode}")
    return parse_index_latest(result.stdout)


def remote_app_server_set_names(host: str, items: list[dict[str, Any]], timeout: int) -> dict[str, Any]:
    payload_items = [
        {
            "threadId": str(item["threadId"]),
            "name": str(item["newTitle"]),
        }
        for item in items
    ]
    payload = base64.b64encode(json.dumps(payload_items, ensure_ascii=False).encode("utf-8")).decode("ascii")
    remote_script = r'''
import base64
import json
import pathlib
import select
import shutil
import subprocess
import sys
import time

items = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
timeout = float(sys.argv[2])
codex = shutil.which("codex")
if not codex:
    for candidate in (
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        "/Applications/Codex.app/Contents/Resources/codex",
        str(pathlib.Path.home() / ".local/bin/codex"),
    ):
        if pathlib.Path(candidate).exists():
            codex = candidate
            break
if not codex:
    raise SystemExit("codex executable not found on remote host")
proc = subprocess.Popen(
    [codex, "app-server", "--listen", "stdio://"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    encoding="utf-8",
    errors="replace",
    bufsize=1,
)

def send(obj):
    proc.stdin.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    proc.stdin.flush()

def collect_until(pending, deadline):
    responses = {}
    stderr_lines = []
    while pending and time.time() < deadline:
        readable, _, _ = select.select([proc.stdout, proc.stderr], [], [], 0.2)
        for stream in readable:
            line = stream.readline()
            if not line:
                continue
            if stream is proc.stderr:
                if len(stderr_lines) < 20:
                    stderr_lines.append(line.rstrip())
                continue
            try:
                message = json.loads(line)
            except Exception:
                continue
            message_id = str(message.get("id") or "")
            if message_id in pending:
                responses[message_id] = message
                pending.remove(message_id)
    return responses, stderr_lines

try:
    init_id = "session-renamer:init"
    send({
        "id": init_id,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "session-renamer", "version": "1.0"},
            "capabilities": {"experimentalApi": True},
        },
    })
    deadline = time.time() + timeout
    responses, stderr_lines = collect_until({init_id}, deadline)
    if init_id not in responses:
        raise RuntimeError("remote app-server initialize timed out")
    init_response = responses[init_id]
    if init_response.get("error"):
        raise RuntimeError(json.dumps(init_response["error"], ensure_ascii=False))

    pending = set()
    id_to_item = {}
    for index, item in enumerate(items):
        request_id = f"session-renamer:set:{index}"
        pending.add(request_id)
        id_to_item[request_id] = item
        send({
            "id": request_id,
            "method": "thread/name/set",
            "params": {"threadId": item["threadId"], "name": item["name"]},
        })

    responses, more_stderr = collect_until(pending, deadline)
    stderr_lines.extend(more_stderr)
    updated = []
    failed = []
    for request_id, item in id_to_item.items():
        response = responses.get(request_id)
        if response is None:
            failed.append({"threadId": item["threadId"], "title": item["name"], "error": "timed out"})
        elif response.get("error"):
            failed.append({"threadId": item["threadId"], "title": item["name"], "error": response["error"]})
        else:
            updated.append({"threadId": item["threadId"], "title": item["name"]})
    print(json.dumps({"updated": updated, "failed": failed, "stderr": stderr_lines}, ensure_ascii=False))
finally:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
'''
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-", payload, str(timeout)]
    result = subprocess.run(
        cmd,
        input=remote_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 10,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ssh exited {result.returncode}")
    return json.loads(result.stdout.strip())


def apply_app_server_items(
    input_path: Path,
    apply_items: list[Any],
    host_filter: set[str],
    include_local: bool,
    timeout: int,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []
    for item in apply_items:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "local")
        if host_filter and host not in host_filter:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "host not selected"})
            continue
        if host in ("local", "localhost") and not include_local:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "local app-server writes disabled"})
            continue
        if not item.get("threadId") or not item.get("newTitle"):
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "missing threadId or newTitle"})
            continue
        grouped.setdefault(host, []).append(item)

    applied = []
    failed = []
    for host, items in sorted(grouped.items()):
        try:
            if host in ("local", "localhost"):
                raise RuntimeError("local app-server apply is intentionally disabled; use codex_app.set_thread_title")
            result = remote_app_server_set_names(host, items, timeout)
            applied.append(
                {
                    "host": host,
                    "count": len(result.get("updated", [])),
                    "updated": result.get("updated", []),
                    "item_failures": result.get("failed", []),
                    "stderr": result.get("stderr", []),
                }
            )
            if result.get("failed"):
                failed.append({"host": host, "count": len(result["failed"]), "error": "one or more thread/name/set calls failed"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"host": host, "count": len(items), "error": str(exc)})

    return {
        "created_at": iso_now(),
        "source": str(input_path),
        "mode": "remote codex app-server thread/name/set",
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "safety": "Uses Codex app-server thread/name/set on the selected remote hosts; does not edit SQLite or transcripts directly.",
    }


def remote_state_cache_set_titles(host: str, items: list[dict[str, Any]], timeout: int) -> dict[str, Any]:
    payload_items = [
        {
            "threadId": str(item["threadId"]),
            "title": str(item["newTitle"]),
        }
        for item in items
    ]
    payload = base64.b64encode(json.dumps(payload_items, ensure_ascii=False).encode("utf-8")).decode("ascii")
    remote_script = r'''
import base64
import datetime as dt
import json
import pathlib
import sqlite3
import sys

items = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
db_path = pathlib.Path.home() / ".codex" / "state_5.sqlite"
if not db_path.exists():
    raise SystemExit(f"state_5.sqlite not found: {db_path}")
stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
backup_path = db_path.with_name(f"state_5.sqlite.bak-session-renamer-{stamp}")

source = sqlite3.connect(str(db_path), timeout=30)
source.execute("PRAGMA busy_timeout=30000")
backup = sqlite3.connect(str(backup_path))
source.backup(backup)
backup.close()

updated = []
missing = []
unchanged = []
try:
    for item in items:
        thread_id = str(item["threadId"])
        title = str(item["title"])
        row = source.execute("SELECT title FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if row is None:
            missing.append({"threadId": thread_id, "title": title})
            continue
        old_title = row[0] or ""
        if old_title == title:
            unchanged.append({"threadId": thread_id, "title": title})
            continue
        source.execute("UPDATE threads SET title = ? WHERE id = ?", (title, thread_id))
        updated.append({"threadId": thread_id, "oldTitle": old_title, "title": title})
    source.commit()
finally:
    source.close()

print(json.dumps({"backup": str(backup_path), "updated": updated, "unchanged": unchanged, "missing": missing}, ensure_ascii=False))
'''
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-", payload]
    result = subprocess.run(
        cmd,
        input=remote_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ssh exited {result.returncode}")
    return json.loads(result.stdout.strip())


def apply_state_cache_items(
    input_path: Path,
    apply_items: list[Any],
    host_filter: set[str],
    include_local: bool,
    timeout: int,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []
    for item in apply_items:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "local")
        if host_filter and host not in host_filter:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "host not selected"})
            continue
        if host in ("local", "localhost") and not include_local:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "local state cache writes disabled"})
            continue
        if not item.get("threadId") or not item.get("newTitle"):
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "missing threadId or newTitle"})
            continue
        grouped.setdefault(host, []).append(item)

    applied = []
    failed = []
    for host, items in sorted(grouped.items()):
        try:
            if host in ("local", "localhost"):
                raise RuntimeError("local state cache apply is intentionally disabled; use codex_app.set_thread_title")
            result = remote_state_cache_set_titles(host, items, timeout)
            applied.append(
                {
                    "host": host,
                    "count": len(result.get("updated", [])),
                    "updated": result.get("updated", []),
                    "unchanged": result.get("unchanged", []),
                    "missing": result.get("missing", []),
                    "backup": result.get("backup"),
                }
            )
            if result.get("missing"):
                failed.append({"host": host, "count": len(result["missing"]), "error": "one or more thread ids were missing from state_5.sqlite"})
        except Exception as exc:  # noqa: BLE001
            failed.append({"host": host, "count": len(items), "error": str(exc)})

    return {
        "created_at": iso_now(),
        "source": str(input_path),
        "mode": "remote state_5.sqlite threads.title fallback",
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "safety": "Backs up state_5.sqlite with SQLite backup API, then updates only threads.title for approved thread ids.",
    }


def append_local_index(home: Path, lines: list[str], stamp: str) -> dict[str, Any]:
    index = home / "session_index.jsonl"
    if not index.exists():
        raise FileNotFoundError(f"local session_index.jsonl not found: {index}")
    backup = index.with_name(f"session_index.jsonl.bak-session-renamer-{stamp}")
    shutil.copy2(index, backup)
    needs_newline = index.stat().st_size > 0 and index.read_bytes()[-1:] != b"\n"
    with index.open("a", encoding="utf-8", newline="\n") as fh:
        if needs_newline:
            fh.write("\n")
        for line in lines:
            fh.write(line)
            fh.write("\n")
    return {"backup": str(backup), "appended": len(lines)}


def append_remote_index(host: str, lines: list[str], stamp: str, timeout: int) -> dict[str, Any]:
    payload = base64.b64encode(("\n".join(lines) + "\n").encode("utf-8")).decode("ascii")
    remote_script = r'''
import base64, json, pathlib, shutil, sys
payload = base64.b64decode(sys.argv[1]).decode("utf-8")
stamp = sys.argv[2]
index = pathlib.Path.home() / ".codex" / "session_index.jsonl"
if not index.exists():
    raise SystemExit(f"session_index.jsonl not found: {index}")
backup = index.with_name(f"session_index.jsonl.bak-session-renamer-{stamp}")
shutil.copy2(index, backup)
needs_newline = index.stat().st_size > 0
if needs_newline:
    with index.open("rb") as fh:
        fh.seek(-1, 2)
        needs_newline = fh.read(1) != b"\n"
with index.open("a", encoding="utf-8", newline="\n") as fh:
    if needs_newline:
        fh.write("\n")
    fh.write(payload)
print(json.dumps({"backup": str(backup), "appended": len([line for line in payload.splitlines() if line.strip()])}, ensure_ascii=False))
'''
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "python3", "-", payload, stamp]
    result = subprocess.run(
        cmd,
        input=remote_script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"ssh exited {result.returncode}")
    return json.loads(result.stdout.strip())


def apply_index_items(
    input_path: Path,
    apply_items: list[Any],
    home: Path,
    p: dict[str, Path],
    host_filter: set[str],
    include_local_index: bool,
    timeout: int,
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    skipped: list[dict[str, Any]] = []
    for item in apply_items:
        if not isinstance(item, dict):
            continue
        host = str(item.get("host") or "local")
        if host_filter and host not in host_filter:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "host not selected"})
            continue
        if host in ("local", "localhost") and not include_local_index:
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "local index writes disabled"})
            continue
        if not item.get("threadId") or not item.get("newTitle"):
            skipped.append({"threadId": item.get("threadId"), "host": host, "reason": "missing threadId or newTitle"})
            continue
        grouped.setdefault(host, []).append(item)

    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    updated_at = iso_now()
    applied = []
    failed = []
    for host, items in sorted(grouped.items()):
        try:
            latest = local_index_latest(home) if host in ("local", "localhost") else remote_index_latest(host, timeout)
            pending = [item for item in items if latest.get(str(item.get("threadId") or "")) != str(item.get("newTitle") or "")]
            already_current = len(items) - len(pending)
            if not pending:
                applied.append({"host": host, "count": 0, "appended": 0, "already_current": already_current, "backup": None})
                continue
            lines = [index_jsonl_line(item, updated_at) for item in pending]
            if host in ("local", "localhost"):
                result = append_local_index(home, lines, stamp)
            else:
                result = append_remote_index(host, lines, stamp, timeout)
            applied.append({"host": host, "count": len(pending), "already_current": already_current, **result})
        except Exception as exc:  # noqa: BLE001
            failed.append({"host": host, "count": len(items), "error": str(exc)})

    return {
        "created_at": iso_now(),
        "source": str(input_path),
        "mode": "append session_index.jsonl records",
        "applied": applied,
        "failed": failed,
        "skipped": skipped,
        "safety": "Backed up each selected session_index.jsonl before appending title records.",
    }


def run_apply_index(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    input_path = Path(args.input) if args.input else p["current"] / "apply_plan.json"
    plan = read_json(input_path, {})
    apply_items = plan.get("apply") if isinstance(plan, dict) else None
    if not isinstance(apply_items, list):
        raise SystemExit("apply plan must contain an apply list")

    report = apply_index_items(
        input_path,
        apply_items,
        home,
        p,
        set(args.host or []),
        args.include_local_index,
        args.timeout,
    )
    output = Path(args.output) if args.output else p["current"] / "apply_index_report.json"
    write_json(output, report)
    print(
        json.dumps(
            {"output": str(output), "applied": report["applied"], "failed": report["failed"], "skipped_count": len(report["skipped"])},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not report["failed"] else 1


def run_apply_app_server(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    input_path = Path(args.input) if args.input else p["current"] / "apply_plan.json"
    plan = read_json(input_path, {})
    apply_items = plan.get("apply") if isinstance(plan, dict) else None
    if not isinstance(apply_items, list):
        raise SystemExit("apply plan must contain an apply list")

    report = apply_app_server_items(
        input_path,
        apply_items,
        set(args.host or []),
        args.include_local,
        args.timeout,
    )
    output = Path(args.output) if args.output else p["current"] / "apply_app_server_report.json"
    write_json(output, report)
    print(
        json.dumps(
            {"output": str(output), "applied": report["applied"], "failed": report["failed"], "skipped_count": len(report["skipped"])},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not report["failed"] else 1


def run_apply_state(args: argparse.Namespace) -> int:
    home = Path(args.codex_home).resolve() if args.codex_home else codex_home()
    p = paths(home)
    ensure_dirs(p)
    input_path = Path(args.input) if args.input else p["current"] / "apply_plan.json"
    plan = read_json(input_path, {})
    apply_items = plan.get("apply") if isinstance(plan, dict) else None
    if not isinstance(apply_items, list):
        raise SystemExit("apply plan must contain an apply list")

    report = apply_state_cache_items(
        input_path,
        apply_items,
        set(args.host or []),
        args.include_local,
        args.timeout,
    )
    output = Path(args.output) if args.output else p["current"] / "apply_state_report.json"
    write_json(output, report)
    print(
        json.dumps(
            {"output": str(output), "applied": report["applied"], "failed": report["failed"], "skipped_count": len(report["skipped"])},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if not report["failed"] else 1


def run_package(args: argparse.Namespace) -> int:
    src = skill_dir()
    output = Path(args.output) if args.output else src.parent / f"{src.name}-{utc_now().strftime('%Y%m%d-%H%M%S')}.zip"
    exclude_parts = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".github", ".venv", "venv", "build", "dist"}
    exclude_suffixes = {".pyc", ".pyo", ".zip"}
    files = []
    for file_path in src.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(src)
        if any(part in exclude_parts for part in rel.parts) or file_path.suffix in exclude_suffixes:
            continue
        files.append((file_path, rel))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for file_path, rel in sorted(files, key=lambda x: x[1].as_posix()):
            info = zipfile.ZipInfo(f"{src.name}/{rel.as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, file_path.read_bytes())
    print(json.dumps({"output": str(output), "file_count": len(files)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare review-first Codex session renames.")
    parser.add_argument("--codex-home", help="Override CODEX_HOME for tests or custom installs.")
    sub = parser.add_subparsers(dest="command", required=True)

    quickstart = sub.add_parser("quickstart", help="Print the skill workflow and user instructions.")
    quickstart.add_argument("--lang", choices=["zh", "en"], default="zh")
    quickstart.add_argument("--format", choices=["text", "json"], default="text")
    quickstart.set_defaults(func=run_quickstart)

    maintenance = sub.add_parser("maintenance", help="Back up previous run and clean generated files.")
    maintenance.add_argument("--cache-days", type=int, default=DEFAULT_RETENTION_DAYS)
    maintenance.add_argument("--keep-backups", type=int, default=DEFAULT_BACKUP_KEEP)
    maintenance.set_defaults(func=run_maintenance)

    discover = sub.add_parser("discover", help="Normalize local/remote session data into sessions.json.")
    discover.add_argument("--local-json", help="Path to codex_app.list_threads JSON output.")
    discover.add_argument("--remote-index", action="append", help="Remote session_index snapshot as HOST=PATH.")
    discover.add_argument("--ssh-host", action="append", help="Read ~/.codex/session_index.jsonl from this SSH host.")
    discover.add_argument("--output", help="Output sessions.json path.")
    discover.add_argument("--enrich-transcripts", action="store_true", help="Add first/latest user-message snippets from transcript files.")
    discover.add_argument("--local-sessions-root", action="append", help="Local .codex/sessions root for transcript enrichment.")
    discover.set_defaults(func=run_discover)

    propose = sub.add_parser("propose", help="Create title proposals from sessions.json.")
    propose.add_argument("--input", help="Input sessions.json path.")
    propose.add_argument("--output", help="Output proposals.json path.")
    propose.add_argument("--backend", choices=["auto", "vllm", "subagent", "heuristic"], default="auto")
    propose.add_argument("--vllm-base-url", default=DEFAULT_VLLM_BASE_URL)
    propose.add_argument("--vllm-api-key", default=DEFAULT_VLLM_API_KEY)
    propose.add_argument("--model", help="vLLM model id. Defaults to first /v1/models result.")
    propose.add_argument("--timeout", type=int, default=30)
    propose.add_argument("--batch-size", type=int, default=1, help="Maximum sessions per vLLM request. Default is 1 for rich-context title generation.")
    propose.add_argument("--subagent-json", help="Validated JSON returned by a subagent.")
    propose.add_argument("--subagent-chunk-size", type=int, default=DEFAULT_SUBAGENT_CHUNK_SIZE, help="Sessions per Codex subagent prompt chunk.")
    propose.add_argument("--include-existing-emoji", action="store_true", help="Also send titles that already start with an emoji to the proposal backend.")
    propose.set_defaults(func=run_propose)

    merge_subagent = sub.add_parser("merge-subagent", help="Merge chunked Codex subagent JSON results and report missing ids.")
    merge_subagent.add_argument("--sessions", help="Input sessions.json path. Defaults to current/sessions.json.")
    merge_subagent.add_argument("--manifest", help="Input subagent_manifest.json path. Defaults to current/subagent_manifest.json.")
    merge_subagent.add_argument("--result-dir", help="Directory containing chunk result JSON files. Defaults to current/subagent_results.")
    merge_subagent.add_argument("--input", action="append", help="Additional subagent JSON result file. Repeatable.")
    merge_subagent.add_argument("--output", help="Output subagent_proposals.json path.")
    merge_subagent.add_argument("--strict", action="store_true", help="Return exit code 2 if any sessions are still missing.")
    merge_subagent.set_defaults(func=run_merge_subagent)

    review = sub.add_parser("render-review", help="Render static HTML review page.")
    review.add_argument("--input", help="Input proposals.json path.")
    review.add_argument("--output", help="Output review.html path.")
    review.set_defaults(func=run_render_review)

    bootstrap = sub.add_parser("bootstrap-review", help="Render the first-run backend configuration page without generating proposals.")
    bootstrap.add_argument("--local-json", help="Optional codex_app.list_threads JSON snapshot to preserve for the Start review button.")
    bootstrap.add_argument("--cache-days", type=int, default=DEFAULT_RETENTION_DAYS)
    bootstrap.add_argument("--keep-backups", type=int, default=DEFAULT_BACKUP_KEEP)
    bootstrap.set_defaults(func=run_bootstrap_review)

    agent_review = sub.add_parser("agent-review", help="Run the scriptable part of a web-requested review cycle.")
    agent_review.add_argument("--request", help="Path to start_review_request.json.")
    agent_review.add_argument("--local-json", required=True, help="Path to codex_app.list_threads JSON output.")
    agent_review.add_argument("--backend", choices=["auto", "vllm", "subagent", "codex", "heuristic"], help="Override request backend.")
    agent_review.add_argument("--subagent-json", help="JSON-only subagent response for Codex backend proposals.")
    agent_review.add_argument("--remote-index", action="append", help="Remote session_index snapshot as HOST=PATH.")
    agent_review.add_argument("--ssh-host", action="append", help="Read ~/.codex/session_index.jsonl from this SSH host.")
    agent_review.add_argument("--vllm-base-url", help="OpenAI-compatible base URL for vLLM/OpenAI proposals.")
    agent_review.add_argument("--vllm-api-key", help="OpenAI-compatible API key. Defaults to SESSION_RENAMER_OPENAI_API_KEY or local-vllm.")
    agent_review.add_argument("--model", help="OpenAI-compatible model id.")
    agent_review.add_argument("--timeout", type=int, default=30)
    agent_review.add_argument("--batch-size", type=int, default=1)
    agent_review.add_argument("--subagent-chunk-size", type=int, default=DEFAULT_SUBAGENT_CHUNK_SIZE, help="Sessions per Codex subagent prompt chunk.")
    agent_review.add_argument("--include-existing-emoji", action="store_true", help="Also send titles that already start with an emoji to the proposal backend.")
    agent_review.add_argument("--port", type=int, default=8765, help="Review server port used in generated commands.")
    agent_review.add_argument("--cache-days", type=int, default=DEFAULT_RETENTION_DAYS)
    agent_review.add_argument("--keep-backups", type=int, default=DEFAULT_BACKUP_KEEP)
    agent_review.set_defaults(func=run_agent_review)

    serve = sub.add_parser("serve-review", help="Serve review.html with a local per-session vLLM regenerate endpoint.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--vllm-base-url", default=DEFAULT_VLLM_BASE_URL)
    serve.add_argument("--vllm-api-key", default=DEFAULT_VLLM_API_KEY)
    serve.add_argument("--model", help="vLLM model id. Defaults to first /v1/models result.")
    serve.add_argument("--timeout", type=int, default=60)
    serve.set_defaults(func=run_serve_review)

    prepare = sub.add_parser("prepare-apply", help="Convert approved.json into apply_plan.json.")
    prepare.add_argument("--approvals", help="Input approved.json path.")
    prepare.add_argument("--output", help="Output apply_plan.json path.")
    prepare.set_defaults(func=run_prepare_apply)

    apply_index = sub.add_parser("apply-index", help="Append approved rename records to local/remote session_index.jsonl with backups.")
    apply_index.add_argument("--input", help="Input apply_plan.json path.")
    apply_index.add_argument("--output", help="Output apply_index_report.json path.")
    apply_index.add_argument("--host", action="append", help="Host to apply. Repeatable. Defaults to all non-local hosts.")
    apply_index.add_argument("--include-local-index", action="store_true", help="Also append local session_index.jsonl records.")
    apply_index.add_argument("--timeout", type=int, default=60)
    apply_index.set_defaults(func=run_apply_index)

    apply_app_server = sub.add_parser("apply-app-server", help="Apply approved remote renames through remote Codex app-server thread/name/set.")
    apply_app_server.add_argument("--input", help="Input apply_plan.json path.")
    apply_app_server.add_argument("--output", help="Output apply_app_server_report.json path.")
    apply_app_server.add_argument("--host", action="append", help="Remote host to apply. Repeatable. Defaults to all non-local hosts.")
    apply_app_server.add_argument("--include-local", action="store_true", help="Reserved for local app-server apply; prefer codex_app.set_thread_title instead.")
    apply_app_server.add_argument("--timeout", type=int, default=120)
    apply_app_server.set_defaults(func=run_apply_app_server)

    apply_state = sub.add_parser("apply-state", help="Fallback: back up and update remote state_5.sqlite thread title cache.")
    apply_state.add_argument("--input", help="Input apply_plan.json path.")
    apply_state.add_argument("--output", help="Output apply_state_report.json path.")
    apply_state.add_argument("--host", action="append", help="Remote host to apply. Repeatable. Defaults to all non-local hosts.")
    apply_state.add_argument("--include-local", action="store_true", help="Reserved for local state cache apply; prefer codex_app.set_thread_title instead.")
    apply_state.add_argument("--timeout", type=int, default=120)
    apply_state.set_defaults(func=run_apply_state)

    package = sub.add_parser("package", help="Export this skill as a zip.")
    package.add_argument("--output", help="Output zip path.")
    package.set_defaults(func=run_package)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
