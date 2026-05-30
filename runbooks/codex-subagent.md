# Codex Subagent Path

Use this only when the user selected Codex subagent, `agent_review_status.json` says `needs_subagent`, or the page created a `subagent_requests/*.json` request.

## Full Review Handoff

1. Read:

```text
~/.codex/tmp/session-renamer/current/subagent_manifest.json
```

2. For each `chunks[]` entry, read its `prompt_path`, spawn a Codex subagent with the configured model preference, and save the JSON-only answer to that chunk's `result_path`.
3. Merge results:

```powershell
python <skill-dir>/scripts/session_renamer.py merge-subagent
```

4. Finish review generation:

```powershell
python <skill-dir>/scripts/session_renamer.py agent-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json --backend subagent --subagent-json ~/.codex/tmp/session-renamer/current/subagent_proposals.json
```

5. Restart or refresh the local review server.

## Per-Item Regeneration Request

1. Read the newest request under:

```text
~/.codex/tmp/session-renamer/current/subagent_requests/
```

2. Run one Codex subagent with the request prompt.
3. Save the JSON-only result to the request's expected `subagent_results/*.result.json` path.
4. Return to the page and use "Check subagent result".

## JSON Shape

Subagents should return only:

```json
{"renames":[{"id":"thread-id","new_title":"Emoji Title","reason":"short reason"}]}
```

Do not invent IDs. Omit uncertain rows; the merge step reports missing IDs.
