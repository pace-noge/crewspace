# Agent Tool Governance & MCP Integrations Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Give each builtin Crewspace agent an explicit, enforceable allowlist of native tools, then extend the same policy model to approved tools discovered from external MCP servers.

**Architecture:** Keep `ToolRegistry` as the canonical native capability catalog. Compute each agent's effective tool surface as `registered tools ∩ agent allowlist ∩ resource authorization`; filter discovery before sending tools to the LLM and enforce the same policy again in the bound runner. Add external MCP servers later as namespaced providers feeding a composite catalog, without routing Crewspace's own native tools through its MCP server.

**Tech Stack:** Python 3.14, FastAPI, Jinja, async SQLAlchemy, Alembic, SQLite/PostgreSQL-compatible repositories, OpenAI-compatible function calling, MCP Python SDK v2, pytest.

---

## Milestone

**Title:** Agent Tool Governance & MCP Integrations

**Repository:** `pace-noge/crewspace`

**Outcome:** Superadmins can decide exactly which tools each builtin agent may discover and execute; every attempt is policy-enforced and auditable. Approved external MCP servers can later contribute namespaced tools under the same deny-by-default policy.

**Milestone completion criteria:**

- Per-agent native Crewspace tool allowlists persist in the database.
- The protected `agent_crewspace` can only be configured by a superadmin.
- Disabled tools are absent from the LLM tool schema and rejected by execution enforcement.
- Existing board/channel/workspace authorization remains mandatory after tool permission succeeds.
- Agent Settings is a dedicated app-shell page with grouped tools and presets.
- Every tool attempt records agent, human initiator, provider, tool, status, duration, and redacted arguments.
- External MCP connections can be registered, tested, disabled, and queried for tools.
- MCP tools are namespaced and disabled by default.
- Changed MCP tool schemas require re-approval before execution.
- Full tests, migration drift check, and rendered UI verification pass.

**Non-goals for the first slice:**

- Do not route native Crewspace tools through the Crewspace MCP server.
- Do not grant tools based only on an agent being builtin/protected.
- Do not support arbitrary remote MCP credentials in plaintext database fields.
- Do not expose MCP configuration to non-superadmins in the first release.
- Do not build workspace-scoped policy overrides until global per-agent policy is proven.

## Delivery sequence

1. **Phase A — Native tool policy foundation**: persistence, repository ports, deny-by-default semantics, filtered discovery, runner enforcement.
2. **Phase B — Agent Settings and auditability**: dedicated UI, presets, protected-agent authorization, tool-call audit log.
3. **Phase C — External MCP providers**: connection management, discovery, namespacing, schema-change approval, composite execution.
4. **Phase D — Hardening**: approvals for destructive tools, SSRF/secret controls, timeouts, response limits, live verification.

---

## Current architecture and constraints

- Canonical native tools are defined in `src/crewspace/application/tools.py::build_registry()`.
- `LLMAgent.from_registry()` currently receives every `registry.list_tools()` item.
- `LLMAgent.on_chat_message()` advertises those tools to the model and calls `runner.run(tool_name, **args)`.
- `ToolRegistry.bind(uow, principal_id)` currently enforces resource authorization through handlers, but has no per-agent capability policy.
- `AgentRegistry.build()` creates local builtin providers from member rows; it is the correct place to load each builtin agent's discoverable tool set.
- Chat service binds tool runners with a human principal for resource scoping. Preserve that human initiator separately from the responding agent identity.
- `src/crewspace/infrastructure/mcp_server.py` already exposes the same native registry to external MCP clients. It should remain an external adapter, not become the builtin agent's route back into Crewspace.
- The protected builtin agent has ID `agent_crewspace`, `pubkey IS NULL`, `backend='llm'`, and `uses_app_llm=1`.
- ORM instances must not cross repository boundaries; keep DTO/domain mappings and SQLite/PostgreSQL compatibility.

## Policy model

For every call, require all three checks:

```text
registered by provider
AND enabled for the responding agent
AND authorized for the target resource under the initiating principal
```

Identity fields must remain distinct:

- `agent_id`: agent selecting/executing the tool.
- `initiator_id`: human whose chat request initiated the run; used for resource scope.
- `provider_id`: `crewspace` for native tools or MCP connection ID.
- `tool_name`: canonical provider-local name.
- `qualified_name`: `<provider_namespace>.<tool_name>` at composite catalog boundaries.

A tool being enabled does not grant access to all boards/channels. A resource authorization failure remains a failure even if the tool is enabled.

## Initial native tool presets

Presets are UI conveniences that write explicit rows; they are not hidden runtime roles.

- **No tools**: empty allowlist.
- **Read only**: `list_boards`, `list_columns`, `find_card` and future tools explicitly classified as read-only.
- **Standard collaborator**: read-only plus normal board/chat collaboration tools such as `create_card`, `move_card`, `comment_card`, `post_message`.
- **All native tools**: every currently registered Crewspace tool; superadmin only.

The exact preset membership must be generated from tool metadata rather than duplicated in templates.

---

### Task 1: Add explicit tool metadata to the canonical registry

**Objective:** Make provider, category, risk, and read/write characteristics first-class metadata without duplicating tool catalogs.

**Files:**
- Modify: `src/crewspace/application/tools.py`
- Test: `tests/test_tools.py`

**Steps:**

1. Add RED tests asserting every registered tool has stable metadata: `provider='crewspace'`, category, risk level, and mutability.
2. Run `uv run pytest tests/test_tools.py -q`; expect failures for missing metadata.
3. Extend `Tool` with fields such as `provider`, `category`, `mutability`, and `risk` using conservative defaults only where safe.
4. Annotate every `build_registry()` registration explicitly.
5. Add helper functions for grouping tools and building presets from metadata.
6. Run the focused tests and ensure no existing MCP schema changes unexpectedly.
7. Commit: `feat: classify agent tools for policy enforcement`.

**Acceptance criteria:**

- Metadata is defined once beside each tool.
- Presets are derived from metadata.
- Tool input schemas and handlers remain unchanged.

---

### Task 2: Persist per-agent native tool permissions

**Objective:** Add queryable, normalized policy storage with deny-by-default semantics.

**Files:**
- Modify: `src/crewspace/domain/entities.py`
- Modify: `src/crewspace/domain/ports.py`
- Modify: `src/crewspace/infrastructure/models.py`
- Modify: `src/crewspace/infrastructure/repositories.py`
- Modify: `src/crewspace/infrastructure/db.py`
- Create: `migrations/versions/<revision>_agent_tool_permissions.py`
- Test: `tests/test_agent_tool_policy.py`
- Test: `tests/test_migrations.py` or existing migration-check coverage

**Proposed table:**

```text
agent_tool_permission
- agent_id          FK member.id, PK component
- provider_type     native|mcp, PK component
- provider_id       crewspace or connection ID, PK component
- tool_name         PK component
- enabled           integer/bool, not null
- approval_mode     automatic|require_approval
- created_at
- updated_at
```

**Steps:**

1. Write repository contract tests for list, replace, enable, disable, and deletion cascade behavior.
2. Verify RED because the port/model/repository do not exist.
3. Add domain value objects/enums for provider type and approval mode.
4. Add repository methods to `AuthRepository` only if agent configuration belongs there; otherwise create a focused `AgentPolicyRepository` on `UnitOfWork` to avoid bloating auth.
5. Add SQLAlchemy model and repository implementation using parameterized SQL/ORM conventions already present.
6. Add an idempotent SQLite-safe Alembic migration with foreign keys and indexes.
7. Decide bootstrap semantics explicitly:
   - Existing builtin agents receive a migration/seeded compatibility allowlist matching their current behavior.
   - Newly created builtin agents default to no tools.
   - Remote agents remain out of scope for local LLM discovery but may have policy rows reserved for future enforcement.
8. Run repository tests and `uv run crewspace-manage makemigrations --check`.
9. Commit: `feat: persist per-agent tool permissions`.

**Acceptance criteria:**

- New builtin agents have no allowed tools unless configured.
- Existing builtin behavior does not silently break after migration.
- Unknown tool names may be stored only if tied to an MCP discovery record; native unknown names are rejected at service validation.

---

### Task 3: Enforce allowlists in the bound runner

**Objective:** Make tool execution impossible when a tool is not enabled, independent of LLM discovery.

**Files:**
- Modify: `src/crewspace/domain/ports.py`
- Modify: `src/crewspace/application/tools.py`
- Test: `tests/test_agent_tool_policy.py`
- Test: `tests/test_tools.py`

**Proposed API:**

```python
registry.bind(
    uow,
    principal_id=initiator_id,
    agent_id=agent_id,
    allowed_tools=allowed_names,
)
```

**Steps:**

1. Add RED tests that an allowed tool executes and a disabled/unknown tool raises a typed `ToolPermissionDenied` before the handler runs.
2. Assert existing board/channel authorization still runs after capability authorization.
3. Add `allowed_tools` and agent identity to `_BoundRunner`.
4. Reject disabled tools before looking up or executing handlers; do not leak handler details for blocked tools.
5. Keep unrestricted binding available only for explicitly trusted internal/MCP adapter paths, named clearly rather than represented by accidental `None` behavior.
6. Update all bind call sites and tests so trust is deliberate.
7. Run `uv run pytest tests/test_tools.py tests/test_agent_tool_policy.py -q`.
8. Commit: `feat: enforce agent tool policy in runner`.

**Acceptance criteria:**

- Direct runner calls cannot bypass policy.
- Resource authorization remains unchanged.
- No implicit `None means all tools` survives in agent-facing paths.

---

### Task 4: Filter LLM tool discovery per builtin agent

**Objective:** Ensure the model only sees tools enabled for that specific agent.

**Files:**
- Modify: `src/crewspace/infrastructure/agents/registry.py`
- Modify: `src/crewspace/infrastructure/agents/llm.py`
- Modify: `src/crewspace/application/services.py`
- Test: `tests/test_agent_registry.py`
- Test: `tests/test_llm_agent.py`
- Test: `tests/test_app.py`

**Steps:**

1. Write a fake-LLM RED test capturing the OpenAI `tools` request and asserting disabled tools are absent.
2. Write a defense-in-depth test where a fake model requests a disabled tool name anyway and execution is rejected/audited.
3. Load allowed native names while building each local builtin agent.
4. Filter `registry.list_tools()` before constructing `LLMAgent`.
5. Carry both `agent_id` and initiating human principal into runner binding at the chat service seam.
6. Ensure two builtin agents with different policies receive different catalogs in the same process.
7. Verify an empty allowlist still permits normal conversational replies by calling the model with no tool definitions (or omitting `tools`/`tool_choice` if required by the OpenAI-compatible gateway).
8. Run focused agent and chat tests.
9. Commit: `feat: scope builtin LLM tools per agent`.

**Acceptance criteria:**

- Disabled tools are not advertised.
- A malicious/hallucinated disabled call cannot execute.
- Agent identity and human initiator are not conflated.

---

### Task 5: Add tool policy application service and presets

**Objective:** Centralize validation, authorization, preset expansion, and atomic replacement of an agent policy.

**Files:**
- Create: `src/crewspace/application/agent_tool_policy.py`
- Modify: `src/crewspace/api/deps.py`
- Test: `tests/test_agent_tool_policy.py`

**Steps:**

1. Add RED tests for protected-agent authorization, non-agent rejection, unknown native tools, duplicate names, and preset expansion.
2. Implement `AgentToolPolicyService.get_effective_policy()` and `replace_native_policy()`.
3. Require superadmin for `agent_crewspace` and, for the first release, all builtin-agent tool configuration.
4. Validate against the live canonical registry before persistence.
5. Replace policy atomically in one UoW transaction.
6. Return a DTO grouped by category with enabled and effective state.
7. Run focused tests.
8. Commit: `feat: add agent tool policy service`.

---

### Task 6: Build dedicated Agent Settings UI

**Objective:** Provide an app-shell settings page with grouped checkboxes, presets, and a clear effective-policy preview.

**Files:**
- Modify: `src/crewspace/api/routers/teams.py` or create `src/crewspace/api/routers/agent_settings.py`
- Create: `src/crewspace/templates/agent_settings.html`
- Modify: `src/crewspace/templates/layout.html`
- Modify: `src/crewspace/templates/management.html` if agent actions live there
- Test: `tests/test_management.py`
- Test: `tests/test_agent_tool_policy.py`

**Routes:**

- `GET /management/agents/{agent_id}/settings`
- `PUT /management/agents/{agent_id}/tools` or form-compatible `POST`

**Steps:**

1. Add RED HTTP/UI tests for superadmin access, non-superadmin 403, protected-agent settings link, grouped tools, current selections, preset controls, and successful persistence.
2. Add a dedicated settings action to each builtin agent's `…` menu; do not put this into a generic management modal.
3. Render identity/status as read-only context and native tools grouped by category.
4. Add presets: No tools, Read only, Standard collaborator, All native tools.
5. Make destructive/high-risk tools visibly marked.
6. Submit explicit selected names; unchecked names become disabled through atomic replacement.
7. Add success feedback and effective tool count.
8. Preserve outside-click menu behavior and app-shell layout conventions.
9. Run management and policy tests.
10. Commit: `feat: add builtin agent tool settings`.

**Acceptance criteria:**

- `agent_crewspace` settings are superadmin-only.
- UI and direct HTTP authorization agree.
- Changes apply to newly built agent providers without requiring a process restart.

---

### Task 7: Add durable tool-call audit records

**Objective:** Record every allowed, blocked, succeeded, and failed agent tool attempt without leaking secrets.

**Files:**
- Modify: `src/crewspace/domain/entities.py`
- Modify: `src/crewspace/domain/ports.py`
- Modify: `src/crewspace/infrastructure/models.py`
- Modify: `src/crewspace/infrastructure/repositories.py`
- Create: `migrations/versions/<revision>_agent_tool_audit.py`
- Modify: `src/crewspace/application/tools.py`
- Create: `src/crewspace/templates/agent_tool_runs.html`
- Test: `tests/test_agent_tool_audit.py`

**Proposed fields:**

```text
agent_tool_call
- id
- agent_id
- initiator_id nullable
- provider_type
- provider_id
- tool_name
- status allowed|blocked|succeeded|failed
- arguments_redacted JSON/text
- result_summary nullable
- error nullable
- duration_ms
- created_at
```

**Steps:**

1. Add RED tests for blocked, successful, and failed calls.
2. Define a centralized argument redactor keyed by schema metadata and sensitive-name patterns (`token`, `secret`, `password`, `authorization`, `key`).
3. Instrument the policy runner around handler execution with monotonic timing.
4. Ensure audit persistence does not turn a successfully committed business operation into a false failure; define transaction behavior deliberately.
5. Add a read-only app-shell run history for superadmins.
6. Cap stored result summaries; never persist full unbounded MCP responses.
7. Run migration, repository, and policy tests.
8. Commit: `feat: audit agent tool executions`.

---

### Task 8: Add MCP connection persistence and secret references

**Objective:** Store external MCP connection configuration without storing reusable credentials in rendered/plaintext fields.

**Files:**
- Modify: `src/crewspace/domain/entities.py`
- Modify: `src/crewspace/domain/ports.py`
- Modify: `src/crewspace/infrastructure/models.py`
- Modify: `src/crewspace/infrastructure/repositories.py`
- Modify: `src/crewspace/config.py`
- Create: `migrations/versions/<revision>_mcp_connections.py`
- Test: `tests/test_mcp_connections.py`

**Proposed tables:**

```text
mcp_connection
- id
- name
- namespace unique
- transport streamable_http|sse|stdio_managed
- endpoint_or_command
- enabled
- auth_secret_ref nullable
- created_by
- created_at
- updated_at

mcp_discovered_tool
- connection_id
- tool_name
- description
- input_schema
- schema_hash
- approval_state pending|approved|changed|disabled
- discovered_at
```

**Steps:**

1. Add RED repository and validation tests.
2. Add models, ports, repository, and migrations.
3. Store only a secret reference; resolve actual credentials from environment/configured secret provider.
4. Normalize and validate namespaces so qualified names are stable and collision-free.
5. Default connections disabled until a successful connection test.
6. Default discovered tools pending/disabled.
7. Run migration checks.
8. Commit: `feat: persist external MCP connections`.

---

### Task 9: Implement safe MCP discovery client

**Objective:** Connect to approved MCP servers, list tools, validate schemas, and detect changes.

**Files:**
- Create: `src/crewspace/infrastructure/mcp_client.py`
- Create: `src/crewspace/application/mcp_connections.py`
- Test: `tests/test_mcp_connections.py`
- Test: `tests/test_mcp_client.py`

**Steps:**

1. Use an in-process fake MCP server for RED tests; do not require network.
2. Implement connection adapters for Streamable HTTP first; add SSE only if SDK compatibility requires it. Defer arbitrary stdio unless the server process is locally managed by Crewspace.
3. Add connect/list-tools timeouts and maximum catalog size/schema size.
4. Validate every discovered tool has a name and object input schema.
5. Compute a stable hash from canonicalized name/description/input schema.
6. Mark a previously approved tool as `changed` and disabled when its hash changes.
7. Persist discovery atomically and retain removed tools as disabled history rather than silently deleting approvals.
8. Run focused tests.
9. Commit: `feat: discover tools from MCP connections`.

---

### Task 10: Add MCP management UI

**Objective:** Let superadmins add, test, enable, disable, inspect, and approve MCP tools from dedicated app-shell pages.

**Files:**
- Create: `src/crewspace/api/routers/mcp_connections.py`
- Create: `src/crewspace/templates/mcp_connections.html`
- Create: `src/crewspace/templates/mcp_connection_form.html`
- Create: `src/crewspace/templates/mcp_connection_tools.html`
- Modify: `src/crewspace/templates/layout.html`
- Test: `tests/test_mcp_connections.py`

**Steps:**

1. Add RED route/UI tests for superadmin-only access and direct 403s.
2. Add a Tools-section navigation entry separate from Workflows and Scheduled instructions.
3. Implement dedicated Add connection, connection detail, and discovered-tools pages.
4. Add Test connection and Refresh tools actions.
5. Show pending/approved/changed/disabled states with schema-change warning.
6. Require explicit approval for each newly discovered or changed tool.
7. Ensure secrets are write-only and never rendered back.
8. Run focused UI tests.
9. Commit: `feat: add MCP connection management`.

---

### Task 11: Merge approved MCP tools into the builtin agent catalog

**Objective:** Present approved namespaced MCP tools to selected builtin agents and route calls through the MCP client under the same policy/audit layer.

**Files:**
- Create: `src/crewspace/application/composite_tools.py`
- Modify: `src/crewspace/infrastructure/agents/registry.py`
- Modify: `src/crewspace/infrastructure/agents/llm.py`
- Modify: `src/crewspace/application/tools.py`
- Test: `tests/test_agent_mcp_tools.py`

**Steps:**

1. Add RED tests for qualified names (`jira.create_issue`), collisions, disabled connections, changed schemas, per-agent denial, timeout, and successful invocation.
2. Build a composite catalog from native registry plus approved MCP discovery records.
3. Sanitize qualified function names for OpenAI-compatible providers while retaining an internal mapping to provider/tool; test provider name limits.
4. Route native calls to `_BoundRunner` and MCP calls to the MCP client through one policy/audit facade.
5. Cap call duration and response bytes; feed a bounded serialized result back to the LLM.
6. Ensure MCP content cannot mutate the system prompt or tool policy.
7. Run agent, MCP, and audit tests.
8. Commit: `feat: expose approved MCP tools to builtin agents`.

---

### Task 12: Add approval mode for destructive tools

**Objective:** Pause execution of configured high-risk tools until an authorized human approves or rejects the exact call.

**Files:**
- Create or modify: `src/crewspace/application/tool_approvals.py`
- Modify: agent tool permission/audit models and repositories
- Create: `src/crewspace/templates/tool_approvals.html`
- Test: `tests/test_agent_tool_approvals.py`

**Steps:**

1. Add RED tests for durable pending state, approve, reject, replay protection, authorization, and restart-safe resume.
2. Reuse workflow approval concepts where possible, but keep agent tool approvals distinct from workflow run approvals.
3. Store the exact provider, tool, schema hash, redacted/display arguments, and encrypted/secure execution payload as needed.
4. Require current schema hash to match at approval time.
5. Resume once only after approval; record final audit status.
6. Add pending approvals to the Agent Settings/Tools area.
7. Run focused and restart simulation tests.
8. Commit: `feat: require approval for sensitive agent tools`.

---

### Task 13: Security and resilience hardening

**Objective:** Prevent MCP connectivity and tool responses from becoming a network, secret, or availability boundary bypass.

**Files:**
- Modify: `src/crewspace/application/mcp_connections.py`
- Modify: `src/crewspace/infrastructure/mcp_client.py`
- Modify: `src/crewspace/config.py`
- Test: `tests/test_mcp_security.py`

**Steps:**

1. Add RED tests for loopback/private/link-local/metadata endpoints according to deployment policy, unsafe redirects, oversized schemas/results, timeouts, and secret leakage.
2. Permit only configured schemes/transports.
3. Resolve DNS and enforce address policy before connection; re-check redirects to mitigate SSRF rebinding/redirect paths.
4. Add per-connection and per-agent concurrency/rate limits.
5. Redact secrets from exceptions, audit arguments, and rendered pages.
6. Treat MCP descriptions/results as untrusted data; never concatenate them into privileged system instructions.
7. Add circuit-breaker/backoff behavior for repeatedly failing providers.
8. Run security tests.
9. Commit: `security: harden MCP tool execution`.

---

### Task 14: End-to-end verification and documentation

**Objective:** Prove native and MCP policy behavior through real application paths and document operations.

**Files:**
- Modify: `README.md` or appropriate docs
- Modify: `tests/test_builtin_llm_smoke.py`
- Modify: `tests/test_mcp_server.py`
- Add integration tests as needed

**Steps:**

1. Verify a builtin agent with no tools can chat but cannot act.
2. Enable one native read tool and prove only that tool appears in the fake LLM request.
3. Enable a native write tool and prove resource authorization still blocks an inaccessible target.
4. Connect an in-process MCP server, discover a tool, approve it, assign it to `agent_crewspace`, and execute it through the full chat path.
5. Change the MCP schema and prove the tool becomes disabled pending re-approval.
6. Verify audit records for allowed, blocked, failed, and approved calls.
7. Render Agent Settings, MCP Connection, tool catalog, and approval pages; verify overflow/popover behavior and form preloading.
8. Run:
   - `uv run pytest tests/test_agent_tool_policy.py tests/test_agent_tool_audit.py -q`
   - `uv run pytest tests/test_mcp_connections.py tests/test_mcp_client.py tests/test_agent_mcp_tools.py tests/test_mcp_security.py -q`
   - `uv run pytest -q`
   - `uv run crewspace-manage makemigrations --check`
   - `git diff --check`
9. Update operational documentation: secret references, connection allowlist, disabling a compromised MCP server, reviewing changed schemas, and recovering pending approvals.
10. Commit: `docs: document agent tool governance and MCP integrations`.

---

## Suggested GitHub issue breakdown

Create these issues under the milestone in this dependency order:

1. **Classify canonical Crewspace tools for policy and risk** — Task 1.
2. **Persist per-agent native tool permissions** — Task 2.
3. **Enforce per-agent allowlists in tool discovery and execution** — Tasks 3–4.
4. **Add Agent Tool Policy service and presets** — Task 5.
5. **Build builtin Agent Settings tool controls** — Task 6.
6. **Audit all agent tool attempts** — Task 7.
7. **Persist and secure external MCP connections** — Task 8.
8. **Discover and revalidate MCP tool schemas** — Task 9.
9. **Build MCP connection and tool approval UI** — Task 10.
10. **Expose approved namespaced MCP tools to builtin agents** — Task 11.
11. **Add durable approval for destructive agent tools** — Task 12.
12. **Harden MCP networking, secrets, limits, and failure handling** — Task 13.
13. **Complete end-to-end verification and operations docs** — Task 14.

Recommended labels: `enhancement`, `agents`, `security`, `backend`, `frontend`, `mcp`, with `security` added to Tasks 2–4 and 7–13.

## Test strategy

- **Unit:** metadata classification, preset expansion, policy decisions, redaction, schema hashing, qualified-name mapping.
- **Repository:** permission replacement, audit writes, MCP connection/discovery persistence, migration behavior.
- **Agent fake-client:** inspect exact OpenAI tool schemas and scripted tool calls without network/API keys.
- **MCP in-process:** use MCP SDK v2 `Client(server)` for discovery/call tests; avoid subprocesses.
- **HTTP/UI:** TestClient through real login, superadmin authorization, dedicated settings forms, direct 403 checks.
- **Integration:** chat request → agent resolution → filtered discovery → policy runner/MCP call → persisted side effect → audit record.
- **Security:** SSRF, secret redaction, schema changes, response limits, timeout, replay, unauthorized settings mutation.
- **Regression:** full `uv run pytest -q`, migration drift, diff hygiene.

## Risks and mitigations

- **Discovery-only enforcement bypass:** enforce again in the runner/composite executor.
- **Agent vs human identity confusion:** carry `agent_id` and `initiator_id` independently through every layer.
- **Existing builtin agents lose all capabilities after migration:** explicitly seed compatibility rows for existing builtin agents; new agents remain deny-by-default.
- **Tool names collide across providers:** require stable provider namespaces and internal qualified names.
- **OpenAI function-name character limits:** add deterministic reversible/safe mapping and tests before MCP integration.
- **MCP schema drift changes behavior after approval:** hash canonical schemas and move changed tools back to disabled/pending.
- **MCP server becomes an SSRF proxy:** restrict schemes/addresses/redirects and support deployment-level endpoint allowlists.
- **Secrets leak through forms/audit/errors:** store references only, render write-only fields, centralize redaction.
- **Long or hanging calls block chat:** enforce timeout, response cap, concurrency limit, and friendly failure result.
- **Approval replay or changed call:** bind approval to call ID, provider, tool, schema hash, and arguments; consume once.
- **Policy cache becomes stale:** load policies when building request-scoped agent providers or add explicit invalidation after settings update; test immediate effect.

## Open questions to decide before Phase C

1. Which secret provider should back `auth_secret_ref` initially: environment-variable names only, an encrypted local secret table, or an external secret manager?
2. Should external MCP connections be global to the installation first, or scoped to a team/workspace from day one? Recommendation: global superadmin-managed connections first; resource scoping can follow.
3. Is arbitrary stdio MCP allowed? Recommendation: only Crewspace-managed commands from an executable allowlist; prioritize Streamable HTTP.
4. Should remote WebSocket agents receive server-enforced allowed-tool policies too? Recommendation: reserve the data model now, implement after builtin enforcement is proven because remote agents execute outside this process.
5. Which native tools belong in the compatibility allowlist for existing `agent_crewspace` and `agent_planner`? Derive from the current registry and review the list before migration.

## Milestone release gates

- [ ] Native allowlist cannot be bypassed via direct runner invocation.
- [ ] Protected Crewspace agent settings are superadmin-only in UI and HTTP.
- [ ] Resource authorization tests still pass for every mutating native tool.
- [ ] New agents and newly discovered MCP tools are deny-by-default.
- [ ] Audit records redact sensitive arguments.
- [ ] Changed MCP schemas revoke approval.
- [ ] MCP calls have timeout and response-size limits.
- [ ] Full suite passes.
- [ ] Alembic models are synchronized with head.
- [ ] Browser/rendered DOM verification is recorded honestly.
- [ ] Operational disable/recovery procedure is documented.
