# Operations

[Русский](Operations-ru.md) · [Wiki home](Home.md)

## Connection and secrets

Choose a profile and check its exact origin. In the overview, inspect connection state and the last actual upstream call. Use **Reconnect** for a replacement connection. The gateway publishes the replacement catalog after verification and lets previously dispatched calls settle. Use **MCP credentials** for an opted-in Basic backend on macOS. It verifies new credentials before committing them to Keychain.

For the local gateway token, use `token-set <symbolic-key>` and configure `access_token_keychain`. Configure an environment fallback only when appropriate. Keep test and production settings and tokens separate.

## Logs and metrics

```sh
ag-mcp-gateway --profile NAME status
ag-mcp-gateway --profile NAME logs --backend SERVER --limit 20
ag-mcp-gateway --profile NAME logs --trace-id TRACE_ID
```

For an authenticated local collector, send the profile token as Bearer to `GET /api/diagnostics/v1/metrics`, `GET /api/technical-logs/v1/status` and `GET /api/technical-logs/v1/events`. Poll forward with `journalEpoch` and `afterSequence` from the last accepted cursor. Use `beforeSequence` to backfill. An expired cursor returns HTTP 409 with `resetCursor`. Record the gap before resuming.

The journals each default to 50 MB of persistent storage and seven days of age retention. Each persistent log file is capped at 50 MB. Startup and minute-based cleanup apply age limits; SQLite may temporarily use a rollback file. Counters reset on process restart. Token usage is a byte-based estimate. The collector should handle missing gauges and explicit loss counters.

## Triage

| Symptom | Check |
| --- | --- |
| No overview | Start the profile and verify the selected loopback port. |
| Browser login repeats | Use username `gateway` and the token for that profile. |
| Upstream 401 or 403 | Check Keychain or the configured token source and upstream access. |
| Tool missing | Confirm backend readiness, profile, deny policy and catalog refresh. |
| Collector sees 409 | Save the gap and reset or backfill from the retained window. |

Observer must support the gateway application type, profile token and technical/diagnostics streams to register it directly. The generic HTTP contract can also be used by an independent local collector.
