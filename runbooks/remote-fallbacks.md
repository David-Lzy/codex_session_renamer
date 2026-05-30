# Remote Fallbacks

Use this only when the user explicitly asks for remote persistence, selected remote hosts need apply handling, or local `codex_app.set_thread_title` cannot address the target host.

## Preferred Remote Method

Use remote Codex app-server first:

```powershell
python <skill-dir>/scripts/session_renamer.py apply-app-server --host server1 --host server2
```

This calls remote `thread/name/set` through `codex app-server --listen stdio://` and writes `apply_app_server_report.json`.

## State Fallback

Use only if app-server cannot update visible remote title state:

```powershell
python <skill-dir>/scripts/session_renamer.py apply-state --host server1 --host server2
```

The script backs up `~/.codex/state_5.sqlite` and updates only `threads.title` for approved thread IDs.

## Index Fallback

Use only if app-server and state fallback are unavailable:

```powershell
python <skill-dir>/scripts/session_renamer.py apply-index --host server1 --host server2
```

The script backs up `~/.codex/session_index.jsonl` before appending title records.

## Safety

- Never edit transcript JSONL files.
- Do not use state or index fallback without user approval.
- If the Desktop sidebar still shows old remote titles, ask the user to collapse/expand or reconnect that host after applying.
