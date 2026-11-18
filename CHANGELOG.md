# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

## [0.4.1] - 2026-11-18

### Fixed

- `ruff check .` now passes. This is a style-only release: the two SIM
  findings are rewritten without changing the checkpoint protocol, recovery
  order, journal semantics, or public API.

### Documentation

- Test counts now state **107 collected** and note that
  `tests/test_cli.py` skips only when `agent-memory-router` is not importable.
  With the router installed, all 107 tests run.
- Added `RELEASE_NOTES_v0.4.1.md`.

## [0.4.0] - 2026-11-18

### Added

- **Checkpoint commit protocol.** The state CAS is now the only commit point.
  `archive_pending=true` is written in the same CAS, and memory is written only
  after it. A CAS loser writes neither memory nor journal.
- **Post-commit journal.** `.longrun/checkpoint_journal.json` (single slot,
  unique temp file, `os.replace`) records the committed checkpoint until the
  archive is confirmed. `resume()` uses it to repair a pending checkpoint.
- `TaskState` fields `schema_version`, `task_epoch`, `checkpoint_key`,
  `commit_seq`, `archive_pending`, and `extra` (unknown-field round-trip).
- `retry_pending_checkpoint()` for archive-only repair and
  `retry_legacy_checkpoint()` for an explicit v3 `pending_checkpoint.json`
  migration.
- `StateStore.save()` gains `expected_task_id`, `expected_task_epoch`, and
  `minimum_revision`; journal removal is guarded by `task_epoch + commit_seq`.
- New errors: `CheckpointArchiveError`, `PendingArchiveError`,
  `StatePendingError`, `ArchivePayloadMissing`, `AmbiguousRecovery`,
  `ActiveTaskExists`, `StateBehindJournal`, `LegacyPendingCheckpoint`.
- `tests/test_v4_protocol.py` and `tests/test_v4_recovery.py` (107 tests
  collected; `test_cli` skips only when the router is not importable).

### Changed

- `checkpoint()` / `complete_phase()` / `finish()`: revoke lease → render →
  state CAS → journal → archive → clear pending → return text. Archive failure
  raises `CheckpointArchiveError` and returns no checkpoint text.
- `resume()` uses `state → journal → memory`, repairs `archive_pending`, keeps
  journal payloads when `state.json` is lost, and never lets `revision` move
  backwards (`minimum_revision`). It also falls back to `checkpoint_key` when a
  cached `checkpoint_id` no longer resolves.
- `recover_from_memory()` validates candidates: active-task protection,
  terminal skipping, highest `commit_seq` for the same task, ambiguity across
  `task_epoch`, and `allow_legacy=True` for v3 markers.
- `require_writable()` persists the revoked lease before raising
  `WriteGateViolation`; `status()` reads disk instead of the in-memory cache.
- Pending is a write hard-stop. `enable_writes`, `require_writable`,
  `save_decision`, `start_task`, and new checkpoints are rejected;
  `begin_turn`, `end_turn`, `status`, and `disable_writes` still work.
- `InMemoryBackend.read_raw()` raises `MemoryBackendError` instead of
  `KeyError` when the raw payload is missing.
- Rules, `.cursorrules`, the agent skill, README, and project index updated to
  the v0.4 protocol; package version bumped to `0.4.0`.

### Fixed

- **Orphan checkpoint after a lost CAS** (published v0.3 target): memory is no
  longer written before the state commit.
- **Marker injection** (published v0.3 target): user text is escaped, marker
  JSON escapes `<!--` / `-->`, and only exactly one marker is trusted.
- A committed checkpoint can no longer be lost when the process dies between
  CAS, journal, and archive.
- A stale journal belonging to another task or epoch is no longer silently
  deleted.
- An archive record without an `id` no longer raises `KeyError`; the checkpoint
  stays pending and fails closed.

### Compatibility

- v3 state files load; missing `task_epoch` / `checkpoint_key` / `commit_seq`
  are populated on the next v4 checkpoint. Future `schema_version` values are
  rejected.
- v3 archives without the new fields are `unverified` and require
  `allow_legacy=True` to participate in recovery.
- A v3 `.longrun/pending_checkpoint.json` is never migrated silently.

### Durability

- Promised level: **process crash**. State and journal use unique temp files
  plus `os.replace`. Power-loss durability (fsync, SQLite
  `synchronous=FULL`) is out of scope for v0.4.

## [0.3.0] - 2026-11-04

### Added

- **Single-turn write lease.** `enable_writes()` now binds permission to the
  current `turn_index` (`write_lease_turn`). `begin_turn()`, `end_turn()`,
  `checkpoint()`, `resume()`, and `recover_from_memory()` all revoke it.
  `require_writable()` rejects a stale lease even when `access_mode` is still
  `writable`.
- `LongRunAgent.begin_turn()` / `end_turn()` for hosts that own the message
  boundary.
- `agent-longrun demo`: a one-command, self-contained
  checkpoint → resume → lease-expiry walkthrough.
- `tests/test_cli.py` and lease/legacy-state tests (52 tests total).
- English `README.md`; the previous Chinese README moves to `README.zh-CN.md`.
- GitHub Actions workflow running the suite on Python 3.10–3.12.

### Changed

- `TaskState` persists `turn_index` and `write_lease_turn`; `from_dict()` defaults
  them for v0.2 state files.
- `checkpoint()` revokes the lease before rendering the machine marker, so a
  persisted marker always records `read_only`.
- `resume()` and `recover_from_memory()` start a new turn and force `read_only`;
  write access must be re-authorized explicitly.
- `memory_client._default_router_path()` now discovers an installed
  `mcp_server` / `memory_core` module via `importlib.util.find_spec`, so pip
  installs no longer require a sibling checkout.
- Package version bumped to `0.3.0`; repository URLs point at
  `Agent-LongRun-Framework-v3`.

### Fixed

- A v0.2 `writable` state loaded by a new process is downgraded to `read_only`
  instead of silently preserving persistent write access.

### Compatibility

- State files without `turn_index` / `write_lease_turn` load as turn 0 with no
  lease and are immediately read-only.
- `home=` remains accepted as an alias for `project_path=`.

## [0.2.0] - 2026-10-25

### Added

- Project-scoped state and memory: `LongRunAgent(root=...)` binds
  `<root>/.longrun/state.json` and `<root>/memory.db`.
- `MCPMemoryBackend(project_path=...)` passes `--project_path` to the MCP child.
- `LocalMemoryBackend(project_path=...)` uses `memory_core.configure()`.
- Router discovery via `AGENT_MEMORY_ROUTER_PATH`, a sibling directory, or an
  installed module.

## [0.1.0] - 2026-10-24

### Added

- 7-state task machine with whitelisted transitions.
- Memory-first `start_task()` and memory-before-output `checkpoint()`.
- Structured checkpoints with a machine-readable `<!-- longrun-state -->` marker.
- `resume()` and `recover_from_memory()`.
- MCP / Local / In-memory backends.
- Runnable examples, rules, and an agent skill.
