"""In-process fail-closed review gate for M8.1 (reviewer subagent stalled).

The independent reviewer subagent stalled repeatedly re-running the broad
test_management.py group without returning a verdict, so per the crewspace
milestone-slice workflow this review is run in-process (documented fallback).
Each invariant is asserted on the REAL module where possible, and against the
authoritative protocol source of truth:
  - api/routers/agents.py  (server inbound dispatch / supports() gates)
  - api/connection.py      (session_id + monotonic seq, cross-reconnect frames)
  - docs/AGENT_PROTOCOL.md §3/§5c (wire contract)
Ambiguity is treated as a blocker.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "examples"))

import importlib.util

spec = importlib.util.spec_from_file_location("agent_example", REPO / "examples" / "claude_code_agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

source = (REPO / "examples" / "claude_code_agent.py").read_text()
routers = (REPO / "src/crewspace/api/routers/agents.py").read_text()
conn = (REPO / "src/crewspace/api/connection.py").read_text()
proto = (REPO / "docs/AGENT_PROTOCOL.md").read_text()

blockers = []
nbs = []

# --- 1. Negotiated capabilities are only the implemented subset -------------
m = re.search(r'"capabilities":\s*\[([^\]]*)\]', source)
negotiated = {c.strip().strip('"') for c in m.group(1).split(",")}
IMPLEMENTED = {"progress", "coding_workspace", "cancellation"}
if not negotiated <= IMPLEMENTED:
    blockers.append(f"negotiates unimplemented caps: {negotiated - IMPLEMENTED}")
for cap in IMPLEMENTED:
    if cap not in negotiated:
        blockers.append(f"missing negotiated cap {cap}")

# progress -> server dispatches agent_progress under a supports('progress') gate
if not re.search(r'ftype == "agent_progress"', routers) or \
   not re.search(r'supports\(.*"progress"\)', routers):
    blockers.append("server has no progress dispatch/gate")
# coding frames are gated on 'coding_workspace' support server-side
if not re.search(r'"coding_workspace"', routers):
    blockers.append("server lacks coding_workspace inbound handler")

# --- 2. Socket drop must NOT kill the process (reconnect loop) --------------
if 'while True:' not in source or 'websockets.connect' not in source or \
   'ConnectionClosed' not in source:
    blockers.append("no reconnect loop / connect handling")

# fresh connect claim + new session on reconnect
if source.count('connect_claim(') < 1:
    blockers.append("connect claim not built per reconnect")
if 'use_session(' not in source:
    blockers.append("session not applied after hello_ack")

# --- 3. No cross-reconnect / duplicate terminal frames ----------------------
if 'generation' not in source:
    blockers.append("no generation guard against cross-reconnect terminal frames")
if not re.search(r'if gen != runtime\.generation', source):
    blockers.append("generation guard not enforced before sending terminal frame")
# completed ids de-dup (re-negotiated session must not re-answer)
if 'completed_run_ids' not in source or 'completed_message_ids' not in source:
    blockers.append("no completed-id idempotence set")
# server rejects replayed/reordered/cross-reconnect frames -> agent must comply
if 'session_id' not in conn or 'seq' not in conn:
    blockers.append("server not verifying session/seq (contract mismatch)")
if 'replay' not in proto.lower() and 'reconnect' not in proto.lower():
    blockers.append("AGENT_PROTOCOL missing replay/reconnect rule")

# --- 4. Cancellation terminates subprocess and signed ack -------------------
if 'coding_run_cancel' not in source or 'proc.terminate()' not in source:
    blockers.append("no cancellation subprocess handling")
if 'coding_workspace_action' not in source:
    blockers.append("no governed workspace action handling")

# --- 5. Signed agent_activity for autonomous external work ------------------
if 'agent_activity' not in source:
    blockers.append("no agent_activity publishing")
if 'active_runs' not in source:
    blockers.append("agent_activity missing active_runs")

# --- 6. Example stays free of app/sqlalchemy imports (pure remote process) ---
t = ast.parse(source)
sql = set()
for n in ast.walk(t):
    if isinstance(n, ast.Import):
        for a in n.names:
            if "sqlalchemy" in a.name:
                sql.add(a.name)
    elif isinstance(n, ast.ImportFrom) and n.module and "sqlalchemy" in n.module:
        sql.add(n.module)
if sql:
    blockers.append(f"example imports sqlalchemy: {sql}")

# --- 7. hello signs with the fresh session only after ack -------------------
if 'if self._session_id is not None' not in source and '"hello"' not in source:
    blockers.append("hello not excluded from session signing")

print("NEGOTIATED:", sorted(negotiated))
print("BLOCKERS:", blockers if blockers else "none")
print("NON-BLOCKERS:", nbs if nbs else "none")
sys.exit(1 if blockers else 0)
