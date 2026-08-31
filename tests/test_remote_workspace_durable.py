"""M8.2 — durable remote workspace lifecycle on the execution host.

These tests exercise the allocator's ability to persist ownership, retained
markers, and removal tombstones across a simulated process restart. Every test
constructs an allocator, performs lifecycle operations, then constructs a
FRESH allocator on the same durable state file to prove restart recovery.
All tests should fail RED before the durable state implementation lands.
"""
from __future__ import annotations

import subprocess
import sys
import json
import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from remote_coding_workspace import CodingWorkspaceDTO, GitWorktreeAllocator


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Durable Test")
    _git(repository, "config", "user.email", "durable@example.test")
    (repository / "README.md").write_text("seed\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "seed")
    return repository


def _make_allocator(tmp_path: Path, *, state_file: str = ".crewspace-workspace-state.json") -> GitWorktreeAllocator:
    return GitWorktreeAllocator(
        repositories={"crewspace": _repository(tmp_path)},
        worktree_root=tmp_path / "worktrees",
        durable_state_path=tmp_path / state_file,
    )


def _fresh_allocator(tmp_path: Path, *, state_file: str = ".crewspace-workspace-state.json") -> GitWorktreeAllocator:
    """Construct a brand-new allocator on the same durable state file."""
    return GitWorktreeAllocator(
        repositories={"crewspace": tmp_path / "repository"},
        worktree_root=tmp_path / "worktrees",
        durable_state_path=tmp_path / state_file,
    )


# ---------------------------------------------------------------------------
# 1. Allocation ownership survives restart
# ---------------------------------------------------------------------------
def test_allocated_workspace_survives_restart(tmp_path: Path):
    alloc = _make_allocator(tmp_path)
    ws = alloc.allocate(repository_id="crewspace", run_id="run_survive")
    assert ws.path.is_dir()

    # Simulate restart: new allocator, same state file
    alloc2 = _fresh_allocator(tmp_path)
    # The original workspace DTO must be reconstructable from durable state.
    # We cannot directly access _allocated_workspaces on the new allocator,
    # but we can prove ownership by issuing a lifecycle action that requires it.
    # Retain requires ownership — if ownership is lost, this raises ValueError.
    assert alloc2.retain(ws) == "retained"


# ---------------------------------------------------------------------------
# 2. Retained workspace is never deleted after restart
# ---------------------------------------------------------------------------
def test_retained_workspace_not_deleted_after_restart(tmp_path: Path):
    alloc = _make_allocator(tmp_path)
    ws = alloc.allocate(repository_id="crewspace", run_id="run_retain")
    (ws.path / "keep.txt").write_text("important\n")
    _git(ws.path, "add", "keep.txt")
    _git(ws.path, "commit", "-m", "important work")
    assert alloc.retain(ws) == "retained"

    # Restart
    alloc2 = _fresh_allocator(tmp_path)
    # Retained workspace must survive: cleanup must raise "retained"
    with pytest.raises(ValueError, match="retained"):
        alloc2.cleanup(ws, discard=True)
    assert ws.path.is_dir()


# ---------------------------------------------------------------------------
# 3. Removal tombstone survives restart
# ---------------------------------------------------------------------------
def test_removed_tombstone_survives_restart(tmp_path: Path):
    alloc = _make_allocator(tmp_path)
    ws = alloc.allocate(repository_id="crewspace", run_id="run_tomb")
    (ws.path / "t.txt").write_text("x\n")
    _git(ws.path, "add", "t.txt")
    _git(ws.path, "commit", "-m", "commit for discard")
    assert alloc.cleanup(ws, discard=True) == "removed"
    assert not ws.path.exists()

    # Restart
    alloc2 = _fresh_allocator(tmp_path)
    # Re-discard must return "already_removed" (tombstone survived)
    assert alloc2.cleanup(ws, discard=True) == "already_removed"


# ---------------------------------------------------------------------------
# 4. Idempotent cleanup after restart (workspace removed before restart)
# ---------------------------------------------------------------------------
def test_idempotent_cleanup_after_restart(tmp_path: Path):
    alloc = _make_allocator(tmp_path)
    ws = alloc.allocate(repository_id="crewspace", run_id="run_idem")
    (ws.path / "f.txt").write_text("y\n")
    _git(ws.path, "add", "f.txt")
    _git(ws.path, "commit", "-m", "commit")
    # Merge the worktree commit back into main so a non-discard cleanup is
    # permitted (cleanup only removes a merged branch unless discard=True).
    repo = tmp_path / "repository"
    _git(repo, "merge", "--no-ff", "--no-edit", ws.branch)
    assert alloc.cleanup(ws) == "removed"

    # Restart
    alloc2 = _fresh_allocator(tmp_path)
    assert alloc2.cleanup(ws) == "already_removed"


# ---------------------------------------------------------------------------
# 5. No cross-tenant path leakage: local paths never in result frames
# ---------------------------------------------------------------------------
def test_no_path_leakage_in_workspace_action_result(tmp_path: Path):
    """The workspace action result frames must not contain local filesystem
    paths. This is already enforced by the existing code, but we encode it
    as a regression guard alongside the durable state work."""
    from claude_code_agent import _workspace_action_response

    alloc = _make_allocator(tmp_path)
    ws = alloc.allocate(repository_id="crewspace", run_id="run_noleak")
    (ws.path / "a.txt").write_text("data\n")
    _git(ws.path, "add", "a.txt")
    _git(ws.path, "commit", "-m", "commit")
    frame = _workspace_action_response(alloc, {
        "request_id": "req_noleak",
        "repository_id": "crewspace",
        "run_id": "run_noleak",
        "branch": ws.branch,
        "action": "retain",
    })
    result_repr = repr(frame)
    assert str(ws.path) not in result_repr, (
        "local workspace path leaked into action result frame"
    )


# ---------------------------------------------------------------------------
# 6. Durable state file is not required (backward compat)
# ---------------------------------------------------------------------------
def test_allocator_works_without_durable_state_path(tmp_path: Path):
    """Existing callers that don't pass durable_state_path must still work."""
    repository = _repository(tmp_path)
    alloc = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
    )
    ws = alloc.allocate(repository_id="crewspace", run_id="run_nostate")
    assert ws.path.is_dir()
    assert alloc.retain(ws) == "retained"


# ---------------------------------------------------------------------------
# 7. Forged state cannot grant ownership outside the configured worktree root
# ---------------------------------------------------------------------------
def test_reconstruction_rejects_path_outside_worktree_root(tmp_path: Path):
    repository = _repository(tmp_path)
    state_path = tmp_path / "state.json"
    root = tmp_path / "worktrees"
    alloc = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=root,
        durable_state_path=state_path,
    )
    legitimate = alloc.allocate(repository_id="crewspace", run_id="run_legit")

    # Create a real but unrelated Git worktree outside the configured root, then
    # forge the durable document to claim allocator ownership of it.
    foreign = tmp_path / "foreign-worktree"
    foreign_branch = "crewspace/run_forged-aaaaaaaaaaaa"
    _git(repository, "worktree", "add", "-b", foreign_branch, str(foreign))
    payload = json.loads(state_path.read_text())
    payload["allocated"].append(
        {
            "repository_id": "crewspace",
            "run_id": "run_forged",
            "path": str(foreign),
            "branch": foreign_branch,
            "base_commit": _git(foreign, "rev-parse", "HEAD"),
        }
    )
    state_path.write_text(json.dumps(payload))

    restarted = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=root,
        durable_state_path=state_path,
    )
    forged = CodingWorkspaceDTO(
        repository_id="crewspace",
        run_id="run_forged",
        path=foreign,
        branch=foreign_branch,
        base_commit=_git(foreign, "rev-parse", "HEAD"),
    )
    with pytest.raises(ValueError, match="not allocated|already removed"):
        restarted.retain(forged)
    assert foreign.is_dir(), "forged external path was mutated or removed"
    # A legitimate recorded workspace still reconstructs normally.
    assert restarted.retain(legitimate) == "retained"


# ---------------------------------------------------------------------------
# 8. Concurrent lifecycle persistence stays valid and complete
# ---------------------------------------------------------------------------
def test_concurrent_allocations_persist_complete_valid_state(tmp_path: Path):
    repository = _repository(tmp_path)
    state_path = tmp_path / "state.json"
    alloc = GitWorktreeAllocator(
        repositories={"crewspace": repository},
        worktree_root=tmp_path / "worktrees",
        durable_state_path=state_path,
    )

    def allocate(index: int) -> CodingWorkspaceDTO:
        return alloc.allocate(repository_id="crewspace", run_id=f"run_{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        workspaces = list(pool.map(allocate, range(12)))

    # No lifecycle action raised, the state file is parseable, and no allocation
    # was lost to a stale concurrent snapshot overwriting a newer one.
    payload = json.loads(state_path.read_text())
    persisted = {(item["run_id"], item["branch"]) for item in payload["allocated"]}
    expected = {(ws.run_id, ws.branch) for ws in workspaces}
    assert persisted == expected


# ---------------------------------------------------------------------------
# 9. Migration compatibility is encoded, not a manual-only check
# ---------------------------------------------------------------------------
def test_workspace_module_is_sqlalchemy_free_and_schema_clean():
    source = Path(__file__).resolve().parents[1] / "examples" / "remote_coding_workspace.py"
    tree = ast.parse(source.read_text())
    bad: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "sqlalchemy" in alias.name:
                    bad.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and "sqlalchemy" in node.module:
            bad.add(node.module)
    assert not bad
    result = subprocess.run(
        [sys.executable, "-m", "crewspace.management.cli", "makemigrations", "--check"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
