# Agent workflows

[Русский](Agents-ru.md) · [Wiki home](Home.md)

1. Determine the target profile and environment from the task. Read `ag-mcp-gateway --profile NAME status`. A backend may be starting, ready, missing credentials, rejected or unavailable.
2. Generate an entry with `ag-mcp-gateway --profile NAME config codex`. Supported generator targets are `codex`, `cursor`, `claude`, `opencode`, `deepseek` and `acp`. The output is a fragment for the target client. Register it where that client expects local MCP entries. Keep the running daemon and generated port aligned.
3. Inspect the exposed catalog using `tools/list`. Continue with the supplied cursor. Search with `gateway_tools_search` when agent discovery is enabled. Respect tool descriptions, schema, expected environment and policy.
4. Use the optional authenticated MCP Bridge entry for administrative tasks. Generate it separately with `config --management codex`. Start with `gateway_inspect` before proposing changes. The same profile token authenticates this connection. Authority to inspect a setting does not imply permission to change it.
5. Use `gateway_read_errors` by backend, tool, trace ID or session tag and `gateway_read_metrics` for diagnostics. A tool's `isError` and the gateway's classified diagnostic code serve different consumers. Treat a transport timeout as an uncertain outcome for a side-effecting operation. Confirm state before retrying.

Use `gateway_skill_search` when skills are enabled. Prefer an approved, usable result and check the current policy before replaying the approach. Drafts require explicit review. Persist only neutral summaries and ordered tool names. Module hooks transform JSON locally. Only explicitly allowed read-only tools may run inside manual chains.

The stdio adapter owns MCP protocol stdout. Run service logs in a separate terminal or read them with `logs`. Never paste an Authorization value, password, session key or raw sensitive result into an incident note.

## Connect to one MCP server

Copy the Streamable HTTP address from the MCP card or read `connections[].mcp_url` in the `status` output. For the synthetic `docs` backend in profile `company-test` with environment `test`, use `http://127.0.0.1:18793/mcp/test/company-test/docs`. The profile key is `test/company-test`, and `docs` is the key in the profile's `backends` object. A profile without an environment uses its name alone as the profile key.

Use the gateway profile token as Bearer authentication if the profile is protected. The gateway handles upstream credentials and applies the same policies, hooks and journaling on both addresses. The per-backend catalog and tool calls use the original upstream names, such as `read_page`. Policies and journal entries still identify the tool as `docs.read_page`. At `/mcp/test/company-test`, the aggregate catalog also uses `docs.read_page`.

Use this command to generate a stdio entry for that server.

```sh
ag-mcp-gateway --profile company-test config --backend docs codex
```

The same generator targets listed above are available with `--backend`. Generate an administrative MCP Bridge entry separately with `--management`. The token applies to the whole profile. An address selects a catalog and a destination, while authorization still follows the profile and its tool policies.
