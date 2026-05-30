# Troubleshooting

Use this only for failures in the local review service, JSON state files, ports, or generated review artifacts.

## Missing Review Page

If `serve-review` exits with `review.html not found`, regenerate the first page:

```powershell
python <skill-dir>/scripts/session_renamer.py bootstrap-review --local-json ~/.codex/tmp/session-renamer/current/local_threads.json
```

Then restart:

```powershell
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/restart_review_server.ps1 -Port 8765
```

## Port 8765 Issues

Check listeners:

```powershell
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
```

Use the restart helper before manually killing processes.

## JSON Decode Errors

- `Unexpected UTF-8 BOM`: JSON readers should use `utf-8-sig`; verify `scripts/session_renamer.py` still does this in `read_json`.
- Malformed `desktop_apply_request.json`: do not guess titles. Ask the user to resubmit from the page unless the valid machine-readable fields can be parsed safely.

## Local LLM Failures

The review service can fall back to heuristic proposals. Do not move title generation into Codex unless the user explicitly chooses Codex subagent.
