---
name: codex-session-emoji
description: Review-first Codex session/thread title organizer. Use when the user asks to add emoji, rename, classify, clean up, review, approve, batch-update, or package Codex session titles across local, pinned, and SSH remote-host sessions. Supports local vLLM or subagent title proposals, transcript-enriched context, interactive HTML approval, local cache/history maintenance, and safe apply through codex_app.set_thread_title.
---

# Codex Session Emoji

## Purpose

Prepare safe Codex session rename proposals, let the user approve them, then apply only approved changes with official Codex thread tools. Proposal generation is content-aware: discovery can enrich `session_index.jsonl` rows from local/remote transcript snippets so vague titles like `查看项目` become specific reviewable titles.

## Activation Brief

Whenever this skill is invoked, first give the user a short explanation of the workflow unless they explicitly asked for a silent/background run. Keep it concise and in the user's language. Use this Chinese default:

```text
我会按“先审核、后应用”的方式处理 Codex 会话改名：
1. 先备份并清理本 skill 上一次生成的临时文件，不碰全局会话数据。
2. 拉取当前 Codex Desktop 可见会话，读取少量脱敏上下文来生成标题建议。
3. 生成/刷新本地审核页，你可以在网页里批准、拒绝或手动编辑标题。
4. 你提交审核后，我才会读取批准结果，并通过 Codex 官方 thread title 工具应用改名。

安全边界：生成审核页不会改名；网页不能直接调用 Codex 改名工具；真正应用前会再次以批准结果为准。
```

If the user asks for commands or wants to run it manually, point them to:

```powershell
python <skill-dir>/scripts/session_renamer.py quickstart
```

## Core Rule

Prefer `codex_app.set_thread_title` for local actual renames. For remote-host sidebar entries that cannot be reached because the local tool has no host parameter, use the remote Codex app-server official method `thread/name/set` through `codex app-server --listen stdio://` after user approval. If that cannot update visible remote sidebar state, use the explicit `apply-state` fallback only after user approval: back up the target `state_5.sqlite` with SQLite's backup API, then update only `threads.title` for approved thread ids. If the remote app-server/state path is unavailable, use the explicit `apply-index` fallback: back up the target `session_index.jsonl`, then append new title records. Never edit transcript files.

## Workflow

### Token-Saving Fast Path

For a web-requested fresh review, prefer the persisted runbook and `agent-review` command instead of retyping the full workflow.

- Runbook: `runbooks/start-review-from-web.md`
- Scriptable workflow entry:

```powershell
python <skill-dir>/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
```

- Restart helper:

```powershell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/restart_review_server.ps1 -Port 8765
```

`agent-review` runs maintenance, restores the web request and Desktop thread snapshot, discovers sessions, proposes titles, renders `review.html`, and writes `agent_review_commands.md`. It cannot call Codex Desktop tools itself, so the agent still must call `codex_app.list_threads` first and save the result as `local_threads.json`. If the selected backend is Codex subagent and no `--subagent-json` is supplied, it writes `subagent_prompt.txt` and `agent_review_status.json` with the exact next command.

1. Locate thread tools.
   - If `codex_app.list_threads` and `codex_app.set_thread_title` are unavailable, search tools for `list_threads set_thread_title Codex thread title rename session`.

2. Run maintenance once at the beginning.

```powershell
python <skill-dir>/scripts/session_renamer.py maintenance
```

   - This backs up the previous active run from `~/.codex/tmp/session-renamer/current`.
   - It cleans only `~/.codex/tmp/session-renamer/current`.
   - It prunes only this skill's cache/backups under `~/.codex/cache/session-renamer`.

3. Collect sessions.
   - For local sessions, call `codex_app.list_threads` with `limit: 50`; write the tool JSON to a temporary file if using the script.
   - For specific titles, also call `list_threads` with `query`.
   - For remote sessions missing from `list_threads`, collect read-only snapshots:

```powershell
ssh -o BatchMode=yes -o ConnectTimeout=10 server1 "cat ~/.codex/session_index.jsonl 2>/dev/null" > server1-session_index.jsonl
```

   - Normalize sources:

```powershell
python <skill-dir>/scripts/session_renamer.py discover --local-json local_threads.json --remote-index server1=server1-session_index.jsonl --enrich-transcripts
```

   - `--enrich-transcripts` reads only short first/latest user-message snippets and cwd metadata from `~/.codex/sessions` plus `~/.codex/archived_sessions`.
   - For `--ssh-host`, enrichment also searches remote `~/.codex/sessions`, `~/.codex/archived_sessions`, and `.jsonl.bak-*` transcript files read-only.
   - It skips environment/app/tool wrapper messages and redacts obvious secret-like values before writing `contextSnippet`.

4. Choose proposal backend.
   - Default: `auto`.
   - `auto`: try local vLLM first; if it fails, create a subagent prompt and heuristic fallback proposals.
   - `vllm`: use an OpenAI-compatible endpoint such as a local vLLM server; by default this script points at `http://127.0.0.1:8000/v1`, and users can override it with `SESSION_RENAMER_OPENAI_BASE_URL`, `--vllm-base-url`, or the review page model settings.
   - `subagent`: write `subagent_prompt.txt`; spawn `gpt-5.3-codex-spark` if available, otherwise `gpt-5.4-mini`; then validate its JSON with `--subagent-json`.
   - In the review page's Advanced tools > Models section, users choose one active proposal backend: `OpenAI API` or `Codex subagent`. The inactive backend's fields are disabled and greyed out.
   - `OpenAI API` enables the OpenAI-compatible Base URL, model id, and API key used by per-item regeneration. Leaving the model blank auto-detects the first `/v1/models` result. The API key is only kept in the browser page memory and is not persisted to `localStorage`.
   - `Codex subagent` enables real dropdowns for the preferred model and fallback model. Known choices include `gpt-5.3-codex-spark`, `gpt-5.4-mini`, `gpt-5.3-codex`, `gpt-5.4`, and `gpt-5.5`; choose `Custom...` to type a future Codex model id, which is passed through if it is a safe model-name string.
   - `heuristic`: deterministic local keyword/emoji proposals for tests or fallback.

```powershell
python <skill-dir>/scripts/session_renamer.py propose --backend auto
```

   - The default vLLM batch size is `1` so rich-context titles are generated one session at a time.
   - Use a larger `--batch-size` only for low-context emoji-only cleanup.

5. Render approval HTML.

```powershell
python <skill-dir>/scripts/session_renamer.py render-review
```

   - Open `~/.codex/tmp/session-renamer/current/review.html`.
   - User approves, rejects, edits titles, or expands advanced options per item.
   - The review UI supports light/dark theme switching and Chinese/English language switching; both preferences persist in browser `localStorage`.
   - `开始审核 / Start review` creates `start_review_request.json` and a copyable prompt for the Codex agent to run a fresh review. The page cannot directly call `codex_app.list_threads`, so the agent still performs the actual discovery/proposal/render steps.
   - The static file supports review and JSON export. For per-item AI regeneration, serve it locally:

```powershell
python <skill-dir>/scripts/session_renamer.py serve-review --port 8765
```

   - Open `http://127.0.0.1:8765/`; each item can regenerate a custom-length/style title through local vLLM and merge it back into the standard title field.
   - In server mode, the user should click `提交审核结果 / Submit approvals`; this writes `approved.json` and `apply_plan.json` into the same `current` directory.
   - Submission also writes `desktop_apply_request.json`. The HTML page cannot directly call Codex tools; the Codex agent must read this request and call `codex_app.set_thread_title` for each item so the current Desktop sidebar receives the live `thread-title-updated` broadcast.
   - After submission, the page generates a single-language copy/paste prompt for the Codex agent. The prompt follows the current Chinese/English UI language, is read-only by default, can be unlocked for manual editing, and can be reset to the current-language default template with `初始化默认 / Reset default`.
   - For title regeneration, `重新生成这一条 / Regenerate this item` calls local vLLM immediately. `请求子agent重生成已勾选 / Request subagent for checked` writes a `subagent_requests/*.json` request and prompt; the Codex agent must spawn a subagent, save `subagent_results/*.result.json`, then the page can merge it with `检查子agent结果 / Check subagent result`.
   - The OpenAI-compatible settings in Advanced tools > Models apply to the per-item regenerate button. The Codex model and fallback fields apply to subagent request generation. Only the selected backend is active.
   - The remote buttons are fallbacks: `远端 app-server fallback` uses remote Codex app-server `thread/name/set`; `持久层状态 fallback` backs up and updates remote `state_5.sqlite`; `索引 fallback` appends `session_index.jsonl`.
   - Remote fallback writes may persist titles for reloads, but they might not refresh the currently open Desktop sidebar unless `codex_app.set_thread_title` is also called through the Desktop main process.
   - In static file mode, user downloads/saves `approved.json` into the same `current` directory, then runs `prepare-apply`.

6. Prepare apply plan.

```powershell
python <skill-dir>/scripts/session_renamer.py prepare-apply
```

7. Apply approved renames.
   - Prefer reading `desktop_apply_request.json`; fall back to `apply_plan.json` if it does not exist.
   - Revalidate current title/fingerprint when possible.
   - For each `apply[]` item, call:

```json
{"threadId":"019e...", "title":"📊 AI_Usage_Dashboard 开发"}
```

   - Skip and report items where the thread tool returns `No AppServerManager registered`.
   - Write `desktop_apply_result.json` with `applied`, `failed`, and `skipped` so the review page can show completion status.
   - The review page treats skipped records with `No AppServerManager registered` as old-batch/currently unavailable items. They are hidden from the default `未完成 / Unfinished` filter and remain visible through the `旧批次/不可见 / Old batch` filter.

8. Apply approved remote host renames through remote app-server.
   - Use this for remote hosts shown in the Codex sidebar when local `set_thread_title` cannot address the host.
   - This sends `thread/name/set` to `codex app-server --listen stdio://` over SSH and writes `apply_app_server_report.json`.

```powershell
python <skill-dir>/scripts/session_renamer.py apply-app-server --host server1 --host server2 --host mac-mini
```

   - It does not edit SQLite, transcript JSONL files, or conversation contents directly.
   - If the desktop sidebar is already holding an in-memory remote list, collapse/expand the host or reconnect once to force refresh.

9. Directly refresh remote visible title state only as a fallback.
   - Use this when the visible remote sidebar still shows old titles after app-server apply.
   - This backs up each selected host's `~/.codex/state_5.sqlite`, then updates only `threads.title` for approved thread ids.

```powershell
python <skill-dir>/scripts/session_renamer.py apply-state --host server1 --host server2 --host mac-mini
```

10. Apply approved remote index entries only as a fallback.
   - Use this only when remote app-server apply is unavailable.
   - The command backs up each selected host's `~/.codex/session_index.jsonl` before appending records.

```powershell
python <skill-dir>/scripts/session_renamer.py apply-index --host server1 --host server2 --host mac-mini
```

   - This writes `apply_index_report.json`.
   - It does not edit SQLite, transcript JSONL files, or conversation contents.

11. Package the skill when requested.

```powershell
python <skill-dir>/scripts/session_renamer.py package
```

## Subagent Path

When the user chooses subagent proposals:

1. Run `propose --backend subagent`.
2. Read `~/.codex/tmp/session-renamer/current/subagent_prompt.txt`.
3. Spawn a subagent with model `gpt-5.3-codex-spark`; fallback to `gpt-5.4-mini`.
   - If the review page wrote custom `preferred_model` and `fallback_model` values in `subagent_requests/*.json`, use those values instead.
4. Require the subagent to return only JSON:

```json
{"renames":[{"id":"thread-id","new_title":"🎙️ Qwen ASR","reason":"short reason"}]}
```

5. Save that JSON as `subagent_proposals.json`.
6. Run:

```powershell
python <skill-dir>/scripts/session_renamer.py propose --backend subagent --subagent-json subagent_proposals.json
```

## Safety

- Apply only entries with `approved=true` in `approved.json`.
- Reject suggestions that lack exactly one leading emoji plus a space.
- Reject suggestions that are missing an emoji prefix, too long, contain URLs/long paths/secret-like terms, or are unrelated to the original title.
- Do not send full transcripts or secrets to vLLM/subagents; use only title, host, cwd basename, short preview, and redacted first/latest user-message snippets.
- Preserve the user's title language, project names, capitalization, and punctuation.
- Do not duplicate existing emoji prefixes.
- Treat remote `session_index.jsonl` as read-only discovery data until the user explicitly asks to apply remote index titles.
- For remote visible sidebar renames, prefer remote app-server `thread/name/set` over direct file edits.
- Use `apply-state` only after approval and only for `threads.title`; never update transcript content.
- When applying remote index titles, only append new JSONL records after creating a timestamped backup.

## Storage

- Cache: `~/.codex/cache/session-renamer/cache.json`
- Current run: `~/.codex/tmp/session-renamer/current`
- Run backups: `~/.codex/cache/session-renamer/backups`
- Apply logs: `~/.codex/cache/session-renamer/apply_logs`

Maintenance keeps the last 20 run backups and cache entries younger than 60 days by default.

## Emoji Hints

- Infra/server: `🖥️`, `🏠`, `🧯`, `🔐`, `🔌`
- Database/Supabase/data: `🗄️`, `📊`
- Video/media/game: `🎬`, `🎞️`, `🎮`
- Docs/content/blog: `📘`, `📝`, `📋`
- Email/Gmail: `📩`
- Voice/ASR/audio: `🎙️`
- Cleanup/files/copy: `📦`, `🗂️`, `🧩`
- UI/settings/terminal: `✨`, `🔤`, `🔄`, `🧭`
- Unknown/general investigation: `❓`, `🔎`
