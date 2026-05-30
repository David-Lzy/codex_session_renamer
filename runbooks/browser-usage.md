# Browser Usage

Use this only when the user asks Codex to open, click, inspect, screenshot, or verify the review page.

## Default

Do not use Browser for the normal flow. Start the server and give the user:

```text
http://127.0.0.1:8765/
```

## When Browser Is Requested

Open the in-app Browser to:

```text
http://127.0.0.1:8765/
```

Keep actions minimal:

- Confirm the page loads.
- Inspect visible status or errors.
- Click only controls the user asked you to operate.
- Do not submit approvals or apply renames unless the user explicitly asks.
