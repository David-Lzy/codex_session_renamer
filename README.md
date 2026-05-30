# Codex Session Renamer

Review-first Codex session title organizer. It proposes clearer titles with emoji prefixes, opens an approval page, and applies only the titles the user approves through Codex's official thread title tools.

中文简述：这是一个 Codex skill，用来批量整理会话标题。流程是“先生成审核页，用户确认后再改名”，不会在生成建议时直接修改会话。

## Features

- Review-first workflow with `review.html` approval UI.
- Bilingual Chinese/English UI, light/dark mode, manual title edits.
- Proposal backends:
  - OpenAI-compatible API such as local vLLM.
  - Codex subagent handoff with a copyable next-step prompt in the review page.
  - Local heuristic fallback.
- Local cache and run maintenance to avoid repeated model calls.
- Safe apply flow through `codex_app.set_thread_title`.
- Optional remote fallback commands for app-server/state/index based title persistence.
- Cross-platform Python scripts for Windows, macOS, and Linux.

## Requirements

- Python 3.10 or newer.
- Codex Desktop or a Codex environment that supports local skills.
- Optional: an OpenAI-compatible model endpoint such as vLLM.
- Optional: PowerShell for `scripts/restart_review_server.ps1`; otherwise use `serve-review` directly.

No third-party Python package is required for the core workflow.

## Install

Clone the repository:

```bash
git clone https://github.com/David-Lzy/codex_session_renamer.git
cd codex_session_renamer
```

Install into your Codex skills directory:

```bash
python scripts/install_skill.py
```

If you already have an installed copy and want to replace it:

```bash
python scripts/install_skill.py --force
```

By default the installer uses `CODEX_HOME` when set, otherwise `~/.codex`, and installs to:

```text
~/.codex/skills/codex-session-emoji
```

## Quickstart

Print the workflow summary:

```bash
python scripts/session_renamer.py quickstart
```

Render the first-run configuration page after the Codex agent saves
`local_threads.json`:

```bash
python scripts/session_renamer.py bootstrap-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
```

Start the review server after a review page has been generated:

```bash
python scripts/session_renamer.py serve-review --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

## Typical Codex Workflow

1. Trigger the `codex-session-emoji` skill in Codex.
2. The agent calls `codex_app.list_threads` and saves the current visible session list to `~/.codex/tmp/session-renamer/current/local_threads.json`.
3. The agent runs:

```bash
python ~/.codex/skills/codex-session-emoji/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
```

4. The script runs maintenance, discovery, proposal generation, and renders `review.html`.
5. The user reviews titles in the browser and submits approvals.
6. The agent reads `desktop_apply_request.json` and applies only approved titles through `codex_app.set_thread_title`.

The web page cannot directly call Codex Desktop title tools. This is deliberate: it keeps the browser UI as a review surface and leaves the actual rename step to Codex tool calls.

## OpenAI-Compatible / vLLM Backend

The default endpoint is:

```text
http://127.0.0.1:8000/v1
```

Override it with an environment variable:

```bash
export SESSION_RENAMER_OPENAI_BASE_URL="http://your-vllm-host:8000/v1"
export SESSION_RENAMER_OPENAI_API_KEY="local-vllm"
```

Or pass CLI options:

```bash
python scripts/session_renamer.py propose --backend vllm --vllm-base-url http://your-vllm-host:8000/v1
```

The base URL is normalized automatically. Values such as `your-vllm-host:8000`
or `http://your-vllm-host:8000` are expanded to
`http://your-vllm-host:8000/v1`.

## Codex Backend Handoff

When the review page is served locally and `Codex subagent` is selected, clicking
`Start review` writes a token-saving chunked handoff:

- `subagent_prompt.txt`: short instructions for the Codex agent.
- `subagent_manifest.json`: prompt chunks and expected result paths.
- `subagent_prompts/chunk-*.prompt.txt`: small subagent prompts, 25 sessions by default.
- `subagent_results/`: where each JSON-only chunk result should be saved.

After chunk results are present, merge them:

```bash
python scripts/session_renamer.py merge-subagent
```

`merge-subagent` writes `subagent_proposals.json`, ignores unknown or malformed
ids, reports missing ids, and creates `subagent_missing_prompt.txt` when another
small follow-up run is needed. If a final `--subagent-json` still misses rows,
`agent-review` keeps the review complete by adding local fallback proposals for
missing sessions.

## Commands

```bash
python scripts/session_renamer.py maintenance
python scripts/session_renamer.py bootstrap-review --local-json local_threads.json
python scripts/session_renamer.py discover --local-json local_threads.json --enrich-transcripts
python scripts/session_renamer.py propose --backend auto
python scripts/session_renamer.py merge-subagent
python scripts/session_renamer.py render-review
python scripts/session_renamer.py serve-review --port 8765
python scripts/session_renamer.py prepare-apply
python scripts/session_renamer.py package --output codex-session-emoji.zip
```

Windows helper to restart the local server:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/restart_review_server.ps1 -Port 8765
```

## Test

```bash
python tests/test_session_renamer.py
```

or:

```bash
python -m unittest discover -s tests
```

## Safety

- Generating proposals does not rename sessions.
- Full transcripts should not be sent to models; only short redacted snippets are used.
- `approved=true` is required before apply planning.
- Remote state/index fallback commands create backups first and should be used only after approval.
- The project avoids external runtime dependencies so it is easy to audit.

## License

MIT. See [LICENSE](LICENSE).
