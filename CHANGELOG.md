# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/).

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
