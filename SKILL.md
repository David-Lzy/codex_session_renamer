---
name: codex-session-emoji
description: Low-token Codex session/thread title organizer. Use when the user asks to start a review, approve/apply reviewed session renames, or use advanced Codex Session Renamer paths. Default flow uses Codex only for list_threads and set_thread_title; local review generation runs in scripts/session_renamer.py.
---

# Codex Session Emoji

## Default Rule

Keep Codex on the two Desktop-only boundaries:

1. Start review: call `codex_app.list_threads`, save the JSON snapshot, run `bootstrap-review`, and start the local review server.
2. Apply approved titles: read `desktop_apply_request.json`, call `codex_app.set_thread_title` for each submitted item, and write `desktop_apply_result.json`.

Do not generate titles in Codex on the default path. The web/local service handles local LLM, heuristic proposals, review UI, and approval files.

`propose` and `agent-review` skip sessions whose current title already starts with an emoji by default. Use `--include-existing-emoji` only when the user explicitly wants to re-review already named sessions.

## Short User Brief

Use the user's language. In Chinese, keep it short:

```text
我会走低 token 流程：先只用 Codex 导出当前会话列表并打开本地审核页；标题生成和审核在本地网页/服务里完成。你提交后，我再只读取批准结果并调用 Codex 官方改名工具实时刷新侧栏。
```

## Start Review

1. Call `codex_app.list_threads` with `limit: 50`.
2. Save the exact tool JSON to:

```text
~/.codex/tmp/session-renamer/current/local_threads.json
```

3. Run:

```powershell
python <skill-dir>/scripts/session_renamer.py bootstrap-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/restart_review_server.ps1 -Port 8765
```

4. Tell the user to open or refresh:

```text
http://127.0.0.1:8765/
```

Do not open/control Browser unless the user explicitly asks you to operate or inspect the page.

If the web page writes `start_review_request.json` and asks for a fresh scriptable review, read `runbooks/start-review-from-web.md`.

## Apply Approved Renames

When the user says they submitted approvals:

1. Read:

```text
~/.codex/tmp/session-renamer/current/desktop_apply_request.json
```

2. For each `items[]` entry, call:

```json
{"threadId":"<threadId>","title":"<newTitle>"}
```

using `codex_app.set_thread_title`.

3. Write:

```text
~/.codex/tmp/session-renamer/current/desktop_apply_result.json
```

with:

```json
{"created_at":"...","applied":[],"failed":[],"skipped":[]}
```

If a tool call returns `No AppServerManager registered`, record that item as `skipped` and continue. For detailed apply handling, read `runbooks/apply-approved.md`.

## On-Demand Paths

Read these files only when the trigger applies:

- `runbooks/codex-subagent.md`: user selects Codex subagent, `agent_review_status.json` says `needs_subagent`, or a `subagent_requests/*.json` request is pending.
- `runbooks/remote-fallbacks.md`: user explicitly requests remote persistence, selects remote hosts for apply, or local `set_thread_title` cannot address the target host.
- `runbooks/troubleshooting.md`: local server fails, JSON parsing fails, port 8765 is stuck, review files are missing, or the page reports an internal error.
- `runbooks/browser-usage.md`: user asks you to click, inspect, screenshot, or verify the review page in the in-app Browser.

## Safety

- Never apply titles before approval.
- Never edit transcript files.
- Prefer `codex_app.set_thread_title` for visible local Desktop updates.
- Remote fallback commands must back up the target state/index first and should only run after explicit user approval.
- Do not send full transcripts or secrets to any model; scripts use redacted short snippets.
