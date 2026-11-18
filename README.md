# Agent-LongRun-Framework

English | [简体中文](README.zh-CN.md)

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-106%20passed-brightgreen)](tests/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio-6f42c1)](https://modelcontextprotocol.io)

Runtime contracts for long-running LLM agents: a 7-state task machine, a
**single-turn write lease**, and project-scoped memory — coupled so that
execution and memory are each other's precondition.

> **Companion MCP server:** [`agent-memory-router-v2`](https://github.com/lss486558354-ship-it/agent-memory-router-v2)
> — project-scoped SQLite memory plus raw data, used as the default memory backend.

> **Core idea: an agent session works like a product review.**
> **Conversational state (read-only)** is where you align on scope — clarify the
> requirements, argue about the design.
> **Execution state (writable)** is the task handoff after the plan is approved —
> go write code, stop brainstorming.
>
> The 7-state machine decides *talk or build*; the single-turn write lease decides
> *may this turn touch files*. Conversation and execution are physically separated,
> so the agent cannot keep editing after the approval expires.

![agent-longrun demo](docs/demo.gif)

*`agent-longrun demo` — read-only → lease → checkpoint → resume → stale lease rejected → finish.*

Not another agent framework. No LLM calls, no orchestration DSL. It is a thin
layer that answers three questions an agent runtime keeps getting wrong:

1. **What state is this task in, and is this transition legal?**
2. **Is the agent allowed to touch files right now?**
3. **If the process dies, where does the real progress live?**

## Why

| State machine alone | Memory alone | Coupled (this repo) |
|---|---|---|
| Checkpoints are local; lost on a new machine | Progress exists, but nothing constrains when it is read or written | Local hot state + cold memory backup, each a recovery path |
| Any state can be jumped to | Any progress can be overwritten | Illegal transitions fail; unfinished work cannot be clobbered |
| Records *what*, not *why* | Records results, not boundaries | Decisions, phases, and gate state are archived together |

The core loop:

```text
start_task → search memory → work under the state machine
          → checkpoint: state CAS first, archive memory second, print last
          → resume / recover on another process or machine
```

## The single-turn write lease (v3)

v2 shipped `enable_writes()` as a persistent switch. That is the wrong default:
the user approves **one** action, and the agent keeps writing files in every
later turn.

v3 makes write permission a lease bound to a conversation turn:

- `TaskState.turn_index` tracks the current turn.
- `enable_writes(reason)` binds `write_lease_turn` to the current turn.
- `begin_turn()` / `end_turn()` / `checkpoint()` / `resume()` /
  `recover_from_memory()` all revoke the lease.
- `require_writable()` checks both `access_mode` and `write_lease_turn`; a stale
  lease raises `WriteGateViolation` even if `access_mode` still says `writable`.
- A legacy v2 state that was `writable` is downgraded to `read_only` on load.

```python
agent.enable_writes("plan approved")
agent.require_writable("edit adapters/xometry.py")   # OK, turn 1

agent.begin_turn("next user message")
agent.require_writable("keep editing")               # raises WriteGateViolation

agent.enable_writes("new plan approved")
agent.require_writable("keep editing")               # OK, turn 2
```

The host is responsible for calling `begin_turn()` when a new user message
arrives — the framework cannot see message boundaries by itself. `resume()`
already includes one `begin_turn`; do not call it twice for the same message.

## Checkpoint commit protocol (v4)

v3 wrote memory first and then committed state. If the CAS lost, the losing
writer had already left an orphan checkpoint in memory. v4 makes **the state CAS
the only commit point**:

```text
revoke lease → render (archive_pending=True)
  → state CAS                         ← commit point; no memory write before this
  → .longrun/checkpoint_journal.json
  → save_memory (archive)
  → clear pending + delete journal
  → return checkpoint text
```

Properties:

- A CAS loser writes neither memory nor journal, so recovery cannot select it.
- `archive_pending=true` is committed in the same write as the checkpoint. If
  the archive fails, `checkpoint()` raises `CheckpointArchiveError` and returns
  no text. `resume()` / `retry_pending_checkpoint()` repairs it.
- Recovery authority is `state → journal → memory`, not "newest memory wins".
- A checkpoint is qualified by `task_epoch + checkpoint_key + commit_seq`;
  `checkpoint_id` is only a best-effort cache. The full key is stored in the
  title/keywords because the router only searches those fields.
- Same task: highest `commit_seq` wins. Different `task_epoch`: ambiguous unless
  you pass a key or `on_ambiguous="latest"`.
- Pending is a write hard-stop: `enable_writes`, `require_writable`,
  `save_decision`, `start_task`, and new `checkpoint` calls are rejected until
  the archive is repaired. Turn lifecycle and `status()` still work.
- Legacy v3 state loads, but v3 archives are `unverified` until
  `allow_legacy=True`. A v3 `pending_checkpoint.json` requires an explicit
  `retry_legacy_checkpoint()` or `clear(force=True)`.

Durability promise: **process crash**, not power loss. State/journal use unique
temp files plus `os.replace`; fsync/SQLite `synchronous=FULL` are out of scope.

## Quickstart

Install the memory router and this framework straight from Git:

```bash
python -m pip install "agent-memory-router @ git+https://github.com/lss486558354-ship-it/agent-memory-router-v2.git"
python -m pip install "agent-longrun-framework @ git+https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3.git"

agent-longrun demo
```

The demo runs the whole loop in a temp directory:

```text
start_task (read-only)
  → enable_writes (unlocks the current turn only)
  → checkpoint (state CAS → journal → archive → print)
  → new Agent resume (back to read-only, next turn)
  → write attempt rejected: the lease did not survive resume
  → enable_writes again
  → finish
```

Expected final line: `OK: checkpoint -> resume -> single-turn write lease -> finish`.

If `agent-longrun` is not on `PATH`, activate the virtualenv you installed into.

### From source

```bash
git clone https://github.com/lss486558354-ship-it/agent-memory-router-v2.git
git clone https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3.git

cd Agent-LongRun-Framework-v3
python -m pip install -e ".[dev]"
python -m pip install -e ../agent-memory-router-v2

python -m pytest tests/ -q                    # 107 passed (router installed)
python examples/01_interrupted_task.py        # in-process backend
python examples/01_interrupted_task.py --mcp  # real MCP stdio
```

The router is located in this order: `AGENT_MEMORY_ROUTER_PATH` → an installed
`memory_core` / `mcp_server` module → a sibling directory → the current working
directory. Both pip installs and side-by-side clones work.

## Architecture

```mermaid
flowchart LR
    U[Task] --> A["LongRunAgent(root=project)<br/>start_task"]

    A -->|invariant 1<br/>search_memory| M[("project memory.db<br/>raw_data/")]
    M -->|summaries + on-demand raw text| A

    A --> S["state machine<br/>in_progress / read_only"]
    S -->|plan approved<br/>enable_writes| W["state machine<br/>writable<br/>lease = current turn"]
    W --> X[Execution]

    X --> C{interrupt or phase done?}
    C -->|no| X
    C -->|yes| P["checkpoint<br/>render + machine marker"]

    P -->|commit point<br/>archive_pending=true| L[("<root>/.longrun/<br/>state.json")]
    P -->|post-commit credential| J[".longrun/<br/>checkpoint_journal.json"]
    P -->|invariant 2<br/>save_memory| M
    M -->|ok| O[clear pending<br/>print checkpoint]
    M -->|fail| F["keep pending<br/>CheckpointArchiveError<br/>print nothing"]

    O --> R[resume / retry_pending_checkpoint]
    F --> R
    J --> R
    R -->|state → journal → memory| S

    R -.local state lost.-> M
```

`LongRunAgent(root=...)` decides two things at once:

- hot state → `<root>/.longrun/state.json`
- the default `MCPMemoryBackend` → launched with `--project_path <root>`, so
  memory lands in `<root>/memory.db`

`resume()` can therefore never mix one project's state with another project's
history.

### State machine

```mermaid
stateDiagram-v2
    [*] --> pending
    pending --> in_progress: start_task
    in_progress --> paused: checkpoint(interrupt)
    in_progress --> blocked: note_blocked
    in_progress --> waiting_verification: checkpoint(waiting_verification)
    in_progress --> completed: finish / checkpoint(complete)
    in_progress --> failed: error
    paused --> in_progress: resume
    blocked --> in_progress: resume
    waiting_verification --> in_progress: send back
    waiting_verification --> completed: accepted
    completed --> [*]
    failed --> [*]
```

Transitions are whitelisted. Anything else raises `TransitionError`; terminal
states have no exits.

## Core invariants

1. **Memory before execution.** `start_task()` calls `search()` before it creates
   state. No memory lookup, no task.
2. **CAS before archive, archive before output.** `checkpoint()` commits state
   first, then writes memory. It returns text only after the archive succeeds;
   on failure it raises `CheckpointArchiveError` and leaves the committed state
   `archive_pending`. CAS losers never write memory.
3. **Write permission expires.** One approval covers one turn. Checkpointing,
   resuming, recovering, and ending a turn all revoke it.
4. **One project, one root.** State and memory share the same `root`; unfinished
   tasks cannot be overwritten by a new `start_task`.

## Usage

```python
from workflow_manager import LongRunAgent

agent = LongRunAgent(root="D:/my-project")   # state + default memory bound here

# 1. Start: search_memory first, then create read-only state
started = agent.start_task("implement the quoting adapter")
print(started.memories)   # project-scoped summaries
print(started.context)    # on-demand raw text (top 1 by default)

# 2. Research done: unlock writes for this turn
agent.enable_writes("read the old plan, moving to implementation")
agent.require_writable("edit adapters/xometry.py")

# 3. Interrupt: state CAS, journal, archive, then output
checkpoint = agent.checkpoint(
    reason="token budget warning",
    summary="Xometry read-only prober is done",
    next_step="wire up Protolabs",
    decisions=["read-only probing first, no login automation"],
)
print(checkpoint.text)    # last line: <!-- longrun-state: {...} -->

# 4. New process, same project root
agent = LongRunAgent(root="D:/my-project")
bundle = agent.resume()
print(bundle.state.status, bundle.state.next_step)

# 5. v3: resume is read-only again — re-authorize before writing
agent.enable_writes("confirmed after resume")
agent.require_writable("continue editing adapters/xometry.py")

# 6. Phase archive / finish
agent.complete_phase("phase 2: Xometry", "read-only probe passed",
                     decisions=["login required"], next_step="phase 3")
agent.finish("all adapters wired and verified")
```

On a new machine, with local state gone (also read-only — re-authorize writes):

```python
agent = LongRunAgent(root="/new/machine")
bundle = agent.recover_from_memory("T_20261024_155703_2f9c")
agent.enable_writes("confirm the recovered next step")
```

## Memory backends

| Backend | Use | Notes |
|---|---|---|
| `MCPMemoryBackend` (default) | production / cross-process | spawns an MCP stdio child per call with `--project_path` |
| `LocalMemoryBackend` | examples / same machine | in-process `memory_core.configure(project_path)` |
| `InMemoryBackend` | tests | records calls so ordering can be asserted |

Protocol: `search / read_raw / save / link`.

```python
MCPMemoryBackend(project_path="D:/my-project")
LocalMemoryBackend(project_path="D:/my-project")

# v0.1 spelling still works
MCPMemoryBackend(home="D:/my-project")
```

`LocalMemoryBackend` binds the project root as module-level global state: do not
switch projects inside one process. Use one `MCPMemoryBackend` per project.

## Set up a real project

```bash
cd D:/my-project
agent-projects init
```

This writes `.mcp.json`, `.cursorrules`, a skill, and `.gitignore`, and creates
`memory.db` / `raw_data/`. Then:

```python
agent = LongRunAgent(root="D:/my-project")
```

The Python API does not depend on `.mcp.json`; it passes `--project_path` to the
MCP child itself. The JSON file is for agents running inside Claude Code or
Cursor. Both point at the same project.

## Design decisions

**Why bind state and memory to the same root?**
A checkpoint restores *this project's* progress. If state lives in A and memory
in B, `resume()` will take A's `next_step` and look it up in B's history, with no
way to tell which side is right.

**Why CAS first, then archive, then print?**
The state CAS is the single commit point. Writing memory first means a failed
CAS leaves an orphan record; printing first means a failed write leaves the chat
log as the only copy. Both are excluded by the fixed order.

**Why a JSON marker inside the checkpoint?**
Recovering from prose forces the model to guess status. The marker lets
`recover_from_memory()` rebuild `TaskStatus / AccessMode / completed_steps`
exactly.

**Why both local state and memory?**
`.longrun/state.json` is the hot path: fast and atomic, and the authority for
recovery. Memory is the cold backup: cross-machine, cross-session. Recovery
reads `state → journal → memory`; "newest memory wins" is deliberately not a
rule, because a committed state can be newer than its archived copy.

**Why read-only by default?**
The most common damage during research is editing before the design is settled.
The gate turns "I approve the plan" into an explicit, audited action.

**Why expire write permission every turn?**
The failure mode is not the first approval — it is every turn after it. A user
approves one edit; the agent keeps writing for the rest of the session. A lease
turns approval into a bounded, auditable event with defined revocation points.

**Why fail on a corrupted state file?**
Treating it as "no task" lets the next `start_task` overwrite real progress.
Stopping is cheaper.

## How this differs

| Project | What it gives you | What this adds |
|---|---|---|
| LangGraph | Graph orchestration, checkpointer | A task-state contract and per-turn write gate enforced outside the graph |
| Temporal | Durable workflow execution | A local, file-based contract an LLM can be held to; no cluster |
| Mem0 / vector stores | Semantic long-term memory | Project-scoped SQLite + raw text, with the *write ordering* as the invariant |
| Claude Code hooks | Host-level tool interception | Backend-agnostic Python API + MCP, usable from any host |

This is deliberately small: state machine, memory client, and glue. It is meant
to be embedded, not to own your runtime.

## Known limitations

- `begin_turn()` must be called by the host on each new user message; the
  framework cannot detect message boundaries.
- `resume()` / `recover_from_memory()` already start a new turn — do not call
  `begin_turn()` again for the same message.
- Every MCP call spawns a child process; high-frequency retrieval needs a
  persistent connection or the local backend.
- `LocalMemoryBackend` uses a process-global project root.
- Keyword extraction is heuristic; there are no embeddings.
- Concurrent writers on one `.longrun/` are serialized by `.longrun/state.lock`;
  `StateConflict` means another writer won the race — reload and reconcile
  instead of retrying a stale snapshot.
- The API is synchronous; wrap it in a thread pool for async hosts.
- Windows consoles need `PYTHONIOENCODING=utf-8`; examples reconfigure stdout.

## Security: previously published targets

V0.3 published two agent-native white-hat targets. V0.4 closes both:

- **Marker injection.** `checkpoint()` escapes `<!--` / `-->` in user-visible
  text and in the serialized marker JSON. `_parse_state_marker()` accepts only
  exactly one marker; multiple markers are not trusted. Regression tests:
  `tests/test_v4_recovery.py::test_marker_injection_is_ignored`.
- **Orphan checkpoint after a failed CAS.** The state CAS is now the only commit
  point, and memory is written after it. A CAS loser writes neither memory nor
  journal. Regression test:
  `tests/test_target2_orphan_checkpoint.py::test_cas_conflict_writes_no_archive_and_recovery_uses_committed_checkpoint`.

If you find a v0.4 bypass, report it via GitHub Security Advisories / private
vulnerability reporting. Do not publish a working PoC before a fix is available.
A same-host, same-user process that can replace `.longrun/state.lock` or edit
`.longrun/state.json` directly remains outside the application-level threat
model.

## Tests

```bash
python -m pytest tests/ -q
```

The suite covers, among others:

- illegal transitions fail, terminal states have no exits
- `start_task`'s first backend call is `search`
- `checkpoint` returns only after the final backend call is `save`; a CAS loser
  writes no memory and no journal
- a post-CAS archive failure leaves `archive_pending=true` and returns no text;
  resume / retry repairs it from archive, journal, or a re-rendered fallback
- recovery follows `state → journal → memory`; active tasks are protected,
  terminal candidates are skipped, and same-task candidates pick the highest
  `commit_seq`
- read-only mode blocks writes; `enable_writes` allows them
- the lease expires on `begin_turn` / `end_turn` / `checkpoint` / `resume` /
  `recover_from_memory`
- a stale lease is rejected even when `access_mode` is still `writable`
- a legacy v2 `writable` state is downgraded on load
- `recover_from_memory()` rebuilds state from the machine marker
- the default backend receives the same `project_path` as `root`
- the MCP child receives `--project_path <root>`
- the router resolves from an installed module or a sibling directory
- `agent-longrun demo` exits 0

## License

MIT
