Runtime contracts for long-running LLM agents: a 7-state task machine, a
single-turn write lease, and project-scoped memory.

v0.4 makes the checkpoint a **commit protocol** instead of two independent
writes.

## Highlights

- **State CAS is the only commit point.** `archive_pending=true` is written in
  the same CAS. Memory is written only after the commit, so a CAS loser writes
  neither memory nor journal. The v0.3 orphan-checkpoint target is closed.
- **Post-commit journal.** `.longrun/checkpoint_journal.json` (single slot,
  unique temp file, `os.replace`) carries the committed checkpoint until the
  archive is confirmed. `resume()` repairs a pending checkpoint from
  `archive → journal → re-rendered state`.
- **Recovery is identity-based.** Qualification is
  `task_epoch + checkpoint_key + commit_seq`; `checkpoint_id` is only a
  best-effort cache. Same task picks the highest `commit_seq`; different epochs
  are ambiguous. Active tasks are protected, terminal candidates are skipped,
  and v3 archives need `allow_legacy=True`.
- **Pending is a write hard-stop.** `enable_writes`, `require_writable`,
  `save_decision`, `start_task`, and new checkpoints are rejected until the
  archive is repaired. Turn lifecycle and `status()` still work.
- **Marker injection closed.** User-visible text and the serialized marker JSON
  escape `<!--` / `-->`; only exactly one marker is trusted.
- **107 tests collected**; `test_cli` skips only when
  `agent-memory-router` is not importable. Includes a dedicated post-CAS
  failure and recovery suite.

## Protocol

```text
revoke lease → render (archive_pending=true)
  → state CAS                         ← commit point
  → .longrun/checkpoint_journal.json
  → save_memory (archive)
  → clear pending + delete journal
  → return checkpoint text
```

An archive failure raises `CheckpointArchiveError` and returns no text;
`resume()` / `retry_pending_checkpoint()` finishes the job.

## Install

```bash
python -m pip install "agent-memory-router @ git+https://github.com/lss486558354-ship-it/agent-memory-router-v2.git"
python -m pip install "agent-longrun-framework @ git+https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3.git"

agent-longrun demo
```

Expected final line:

```text
OK: checkpoint -> resume -> single-turn write lease -> finish
```

## Compatibility

- v3 state files load; missing `task_epoch` / `checkpoint_key` / `commit_seq`
  are added by the next v4 checkpoint.
- A v3 `.longrun/pending_checkpoint.json` is never migrated silently. Use
  `retry_legacy_checkpoint()` or `clear(force=True)`.
- v3 checkpoint markers are `unverified` and excluded unless
  `allow_legacy=True`.
- Durability promised in v0.4 is **process crash**, not power loss. fsync and
  SQLite `synchronous=FULL` are out of scope.

## Security

The two v0.3 white-hat targets are closed in v0.4:

- **Target 1 — marker injection.** Regression test:
  `tests/test_v4_recovery.py::test_marker_injection_is_ignored`.
- **Target 2 — orphan checkpoint after a failed CAS.** Regression test:
  `tests/test_target2_orphan_checkpoint.py::test_cas_conflict_writes_no_archive_and_recovery_uses_committed_checkpoint`.

Report a v0.4 bypass through GitHub Security Advisories / private
vulnerability reporting. Do not publish a working PoC before a fix is
available. Same-host, same-user replacement of `.longrun/state.lock` or direct
edits to `.longrun/state.json` remain outside the application-level threat
model.

## Links

- Changelog: https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3/blob/main/CHANGELOG.md
- Protocol rules: `rules/02-checkpoint-archive.md`
- Quickstart: https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3#quickstart
