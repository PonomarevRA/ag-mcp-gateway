# Agent workflows

[Русский](Agents-ru.md) · [Wiki home](Home.md)

1. Determine the target profile and environment from the task. Read `ag-mcp-gateway --profile NAME status`. A backend may be starting, ready, missing credentials, rejected or unavailable.
2. Generate an entry with `ag-mcp-gateway --profile NAME config codex`. Supported generator targets are `codex`, `cursor`, `claude`, `opencode`, `deepseek` and `acp`. The output is a fragment for the target client. Register it where that client expects local MCP entries. Keep the running daemon and generated port aligned.
3. Inspect the exposed catalog using `tools/list`. Continue with the supplied cursor. Search with `gateway_tools_search` when agent discovery is enabled. Respect tool descriptions, schema, expected environment and policy.
    - The gateway orders `tools/list` by completed tool requests since profile startup, excluding policy denials. Send `params._meta = {"ag-mcp-gateway/sort": "name"}` on the first request for alphabetical order. Continue with the returned cursor. Start a fresh list if a catalog change or snapshot eviction invalidates the cursor.
    - `gateway_tools_search` defaults to `sort: "usage"` and also supports `"relevance"` and `"name"`. The sort applies to matching tools. Skills retain their relevance order.
4. Use the optional authenticated MCP Bridge entry for administrative tasks. Generate it separately with `config --management codex`. Start with `gateway_inspect` before proposing changes. The same profile token authenticates this connection. Authority to inspect a setting does not imply permission to change it.
5. Use `gateway_read_errors` by backend, tool, trace ID or session tag and `gateway_read_metrics` for diagnostics. A tool's `isError` and the gateway's classified diagnostic code serve different consumers. Treat a transport timeout as an uncertain outcome for a side-effecting operation. Confirm state before retrying.

Use `gateway_skill_search` when skills are enabled. Prefer an approved, usable result and check the current policy before replaying the approach. Drafts require explicit review. Persist only neutral summaries and ordered tool names. Module hooks transform JSON locally. Only explicitly allowed read-only tools may run inside manual chains.

The stdio adapter owns MCP protocol stdout. Run service logs in a separate terminal or read them with `logs`. Never paste an Authorization value, password, session key or raw sensitive result into an incident note.

## Start a local MCP that needs Docker

The gateway launches configured stdio MCP commands. It detects a `docker` command automatically. For a wrapper that uses Docker, set `"requires_docker": true` on its stdio backend. A wrapper uses `docker` from its environment's `PATH` for the readiness check. Pass any required `DOCKER_HOST` or `DOCKER_CONTEXT` through `env_from` so the probe and the MCP use the same engine.

Call `gateway_backend_startup_status` on the profile's main MCP endpoint. If it returns `action: start_docker`, start Docker or restore engine access, then refresh `tools/list`. The gateway waits in the background and starts the configured MCP when the engine responds. `gateway_inspect` on the management endpoint also reports the recovery action. If the client caches tool lists, refresh or reconnect the client after the backend is ready.

## Reuse MCP workflows

For a suggested Code Atlas `list_projects → search_nodes → describe_node` chain, pass `data: {"project": "ag-design", "query": "Card"}` to `gateway_module_run`. The module resolves the project by exact name and describes the first match using the snapshot returned by search. A missing or ambiguous project or an empty search stops the chain before the next call. Drafts for other integrations still accept separate arguments per step.

For the chain suggestion pilot, set `"chain_suggestions": true` and `"modules_enabled": true` on a profile with the authenticated management bridge. Include eligible tools in `module_chain_tools`. Give each manual workflow attempt its own ID and pass `params._meta = {"ag-mcp-gateway/task-id": "routine-attempt-42"}` on each call in that attempt. A shared W3C trace ID also works when the task ID is absent. After at least two matching sequential attempts, call `gateway_chains_suggest` with the first tool in the workflow. Review the returned module draft, save it with the current module revision, and use the proposed `inputs` when present. For drafts that take `args_1`, `args_2`, etc., supply fresh arguments and add any result bindings they need.

When a read-only workflow or JSON transformation is likely to recur, save it as a module in the target profile. Look for a matching module first. Use direct calls for one-off requests. The profile must have MCP Bridge and modules enabled.

1. Call `gateway_modules_list` on `/manage/<profile>` to read existing definitions and the current revision. Reuse a module that fits.
2. If no module fits and the agent has permission to manage modules, save one by passing `module` and `expected_revision` to `gateway_module_save`. Reference parameters through `/input` and keep the definition limited to steps and transformation rules. Pass working values at runtime and resolve credentials through the profile's configured secret source.
3. Try the workflow with test data first. Call `gateway_module_run` with `id`, `data`, and the current `expected_revision`. On a revision conflict, reload the catalog and check that the definition still fits. Each `call` step must be explicitly allowed in the profile's `module_chain_tools` and advertise `readOnlyHint: true`. The gateway runs up to four sequential calls under its usual tool policies.

To change a tool policy, call `gateway_inspect` and then `gateway_set_tool_policy` with the current `expected_revision`. Saves from MCP Bridge and the dashboard apply to subsequent calls in the open session. Read `tools/list` again if your client caches the catalog.

For example, a `trace_search` module takes a `trace_id`, maps it to the search arguments for Kibana and the log server, and returns their results under `kibana` and `logs`. Read the actual tool and argument names from the profile's catalog. One `gateway_module_run` call performs both searches. An error from the first source stops the workflow.

To call a module on the aggregate `/mcp/<profile>` endpoint, open its saved definition on **Modules**, specify the required input fields under **MCP publication**, and select **Publish as a read-only tool**. For `trace_search`, use `{"trace_id":"string"}`. Read `tools/list` in the open session, then call `trace_search` with `{"trace_id":"…"}`. Both replies are returned in `structuredContent.result`. Unpublish the tool before editing its saved definition.

## Connect to one MCP server

Copy the Streamable HTTP address from the MCP card or read `connections[].mcp_url` in the `status` output. For the synthetic `docs` backend in profile `company-test` with environment `test`, use `http://127.0.0.1:18793/mcp/test/company-test/docs`. The profile key is `test/company-test`, and `docs` is the key in the profile's `backends` object. A profile without an environment uses its name alone as the profile key.

Use the gateway profile token as Bearer authentication if the profile is protected. The gateway handles upstream credentials and applies the same policies, hooks and journaling on both addresses. The per-backend catalog and tool calls use the original upstream names, such as `read_page`. Policies and journal entries still identify the tool as `docs.read_page`. At `/mcp/test/company-test`, the aggregate catalog also uses `docs.read_page`.

Use this command to generate a stdio entry for that server.

```sh
ag-mcp-gateway --profile company-test config --backend docs codex
```

The same generator targets listed above are available with `--backend`. Generate an administrative MCP Bridge entry separately with `--management`. The token applies to the whole profile. An address selects a catalog and a destination, while authorization still follows the profile and its tool policies.

Use `gateway_tools_find_keywords` on the management MCP when agent discovery is enabled. For example, send `{"query":"grafana traceId"}` for relevance-ranked matches, or add `"backend":"grafana"` to restrict the search. Results include descriptor keywords, observed technical labels and related searches. Observations come from field names in successful calls and structured results, expire after an hour without observations or on profile restart, and contain no payload values. The **Keywords** disclosure on each Tools card loads these labels on demand.

For trace searches, add `"draft_trace_module":true` to request an unsaved module that maps a `trace_id` input into compatible allowlisted read-only tools. Review the returned argument mapping before saving it with `gateway_module_save`.
