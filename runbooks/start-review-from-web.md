# Start Review From Web Request

This runbook keeps the Codex agent workflow short. Use it when the review page writes `start_review_request.json` and asks the agent to start a fresh review.

## Files

- Request: `~/.codex/tmp/session-renamer/current/start_review_request.json`
- Desktop snapshot: `~/.codex/tmp/session-renamer/current/local_threads.json`
- Main script: `<skill-dir>/scripts/session_renamer.py`
- Restart helper: `<skill-dir>/scripts/restart_review_server.ps1`

## Step 1: Capture Desktop Threads

Use the Codex Desktop thread tool, not shell, to call `codex_app.list_threads`. Save the JSON result to `local_threads.json`.

Use the highest accepted limit. If `50` is rejected, retry with `10`.

## Step 2: Run Scriptable Workflow

For OpenAI-compatible or local vLLM proposals:

```powershell
python <skill-dir>/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
```

For Codex subagent proposals, first prepare the prompt:

```powershell
python <skill-dir>/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json --backend subagent
```

Then read `subagent_prompt.txt`, spawn the configured Codex subagent, save the JSON-only answer to `subagent_proposals.json`, and finish:

```powershell
python <skill-dir>/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json --backend subagent --subagent-json ~/.codex/tmp/session-renamer/current/subagent_proposals.json
```

The command runs maintenance, restores the request/snapshot, discovers sessions, proposes titles, renders `review.html`, and writes `agent_review_commands.md`.

## Step 3: Restart Local Review Server

```powershell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/restart_review_server.ps1 -Port 8765
```

Then tell the user to refresh `http://127.0.0.1:8765/`.

## Boundaries

- This workflow never applies renames.
- The web page cannot call `codex_app.list_threads` or `codex_app.set_thread_title`.
- Applying approved titles remains a separate Desktop-tool step through `codex_app.set_thread_title`.
- Do not send full transcripts or secrets to models; the script uses redacted title, cwd basename, preview, and short context snippets.
