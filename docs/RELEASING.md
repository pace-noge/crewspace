# Releasing

This runbook describes how Crewspace milestones are versioned, tagged, and
released. Crewspace ships in milestone slices; each slice follows the same
**verified-slice flow** (RED → GREEN → bounded gate → independent fail-closed
review → separate feature/documentation commits → push) before it counts as
released.

## Versioning

The package version lives in `pyproject.toml` under `[project] version` (the
single source of truth for the Python distribution). Crewspace follows
semantic-style versioning at the release level:

- **MAJOR** — breaking changes or a major capability line.
- **MINOR** — new milestone features (backward compatible).
- **PATCH** — fixes and small corrections.

The `version = "0.1.0"` bump in `pyproject.toml` is applied on a release. Keep
`README.md` and `docs/RELEASE_M<MILESTONE>.md` in sync with the milestone that
the bump closes.

## Milestone tracking and release records

- `PLAN.md` — the milestone tracker (each slice row: `PLANNED → IN PROGRESS → DONE`).
- `PLAN_M<M>_<NAME>.md` — canonical detailed plan with per-slice acceptance and
  an append-only progress log.
- `PROGRESS.md` — fresh-session handoff that records the latest verified
  implementation commit and current milestone state.
- `docs/RELEASE_M<M>.<N>.md` — per-release acceptance record. See the existing
  `docs/RELEASE_M6.8.md` and `docs/RELEASE_M6.7.md` for the established format
  (Summary, Acceptance checklist, What shipped, Verification, Architecture
  notes, and the `Tag:` line).

## The verified-slice flow

Every feature slice follows the same discipline:

1. **RED** — write a failing test that expresses the required behavior. Run it
   and watch it fail (missing feature, not a typo).
2. **GREEN** — write the minimal code to pass it. Run it green.
3. **Bounded gate** — run only focused/bounded test groups, not the whole suite:
   `uv run pytest -q <group>`, then `makemigrations --check`, `compileall`,
   `git diff --check`, and an added-line security scan.
4. **Independent fail-closed review** — a reviewer with fresh context inspects
   the diff and returns `BLOCKERS:` / `NON-BLOCKERS:`. Ambiguity is a blocker.
   The slice is not committed or pushed while any blocker remains.
5. **Separate commits** — one `feat(...)` implementation commit and one
   `docs(m<M>)` tracking/documentation commit.
6. **Push** only after every gate above is clean and `BLOCKERS: none`.

Mark `DONE <n>/<n>` in `PLAN.md`/`PLAN_M<M>_<NAME>.md` only after the review
returns `BLOCKERS: none`, then record the verified implementation commit hash in
`PROGRESS.md`.

## Cutting a tagged release

When an entire milestone (all its slices) is done and pushed:

```bash
# 1. Ensure master is clean and synced.
git status -sb

# 2. The milestone implementation commit is the last pushed feat(...) commit.
#    Note its hash (e.g. from git log or PROGRESS.md).

# 3. Bump the version in pyproject.toml (MAJOR.MINOR.PATCH).
git add pyproject.toml PLAN.md PLAN_M<M>_<NAME>.md PROGRESS.md docs/RELEASE_M<M>.N.md
git commit -m "release: crewspace <version>"

# 4. Tag the milestone implementation commit (not the bump commit).
#    The existing tags milestone-m6.7 and milestone-m6.8 follow this convention.
git tag -a milestone-m<M>.<N> -m "crewspace <version>"

# 5. Push master and the tag.
git push origin master
git push origin milestone-m<M>.<N>
```

The milestone tag points at the verified implementation commit, not at the version-bump
commit. Existing tags confirm this: `milestone-m6.7` is at `a2bec64` (implementation)
and `milestone-m6.8` is at `6b734bc` (implementation).

## Post-release

- Confirm `git status -sb` is clean and `master` is synced with `origin/master`.
- Confirm the release tag is present: `git tag -l`.
- The ops acceptance gate (M9.7) ties every M9 slice invariant into one bounded
  suite; run it before declaring M9 production-ready.
