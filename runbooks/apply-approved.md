# Apply Approved Renames

Use this only after the user has submitted approvals in the review page.

## Inputs

- Request: `~/.codex/tmp/session-renamer/current/desktop_apply_request.json`
- Output: `~/.codex/tmp/session-renamer/current/desktop_apply_result.json`
- Tool: `codex_app.set_thread_title`

## Steps

1. Read `desktop_apply_request.json` with UTF-8 or UTF-8-SIG.
2. Do not regenerate or edit titles.
3. For each `items[]` entry with `status: pending`, call `codex_app.set_thread_title` using:

```json
{"threadId":"<threadId>","title":"<newTitle>"}
```

4. Record successes in `applied` with `threadId`, `host`, `oldTitle`, and `newTitle`.
5. If the tool returns `No AppServerManager registered`, record the item in `skipped` with that reason and continue.
6. Record other exceptions in `failed` with the error message and continue.
7. Write `desktop_apply_result.json`:

```json
{
  "created_at": "2026-01-01T00:00:00Z",
  "applied": [],
  "failed": [],
  "skipped": []
}
```

## Notes

- `apply_count: 0` is valid; write an empty result file.
- Remote fallback persistence is not part of this path. Read `runbooks/remote-fallbacks.md` only if the user asks for it or remote hosts need fallback handling.
