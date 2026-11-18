# v0.4.1 — Release Hygiene

This is a style/documentation patch on top of v0.4.0. It does **not** change the
checkpoint commit protocol, recovery order, journal semantics, state schema, or
public API.

## Fixed

- `ruff check .` now passes.
  - `state_machine.py`: merged the nested journal comparison condition
    (SIM102).
  - `workflow_manager.py`: replaced best-effort `try/except/pass` with
    `contextlib.suppress(Exception)` (SIM105).
- Behavior is unchanged: a failed journal write is still best-effort, and a
  conflicting journal is still rejected with `StateConflict`.

## Documentation

- Test counts now state **107 collected**. `tests/test_cli.py` skips its CLI
  assertion only when `agent-memory-router` is not importable; with the router
  installed, all 107 tests run.
- README (English/Chinese), `CHANGELOG.md`, `PROJECT_INDEX.md`, and the v0.4.0
  release note were updated to the conditional test-count wording.

## Compatibility

- v0.4.0 state files and journals are unchanged and remain readable.
- No migration is required.
- The v0.4 durability boundary is unchanged: process crash only; power-loss
  durability (fsync / SQLite `synchronous=FULL`) remains out of scope.

## Verification

```bash
python -m pytest tests/ -q
# 107 passed when the router is importable;
# 106 passed + 1 skipped when it is not.

python -m ruff check .
python -m cli demo
python examples/01_interrupted_task.py
```

Expected demo line:

```text
OK: checkpoint -> resume -> single-turn write lease -> finish
```
