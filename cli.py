"""Command line entry point for Agent-LongRun-Framework v3.

    agent-longrun demo

The demo is intentionally self-contained: it creates a temporary project,
runs a checkpoint, opens a new agent to resume it, and shows that the
single-turn write lease does not survive the resume.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
from pathlib import Path

from memory_client import LocalMemoryBackend, MemoryBackendError
from workflow_manager import LongRunAgent, TaskStatus, WriteGateViolation


def _utf8_console() -> None:
    """Windows 控制台默认 GBK，中文标点会把输出打断。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def run_demo() -> int:
    _utf8_console()
    home = Path(tempfile.mkdtemp(prefix="longrun-demo-"))
    print(f"project root: {home}")

    memory = LocalMemoryBackend(project_path=home)
    agent = LongRunAgent(memory, root=home)

    print("\n[1] start_task: read-only by default")
    started = agent.start_task("demo: interrupted long task")
    print(started.summary)
    try:
        agent.require_writable("demo write")
    except WriteGateViolation as exc:
        print(f"gate: {exc}")

    print("\n[2] enable_writes: lease for this turn only")
    state = agent.enable_writes("plan confirmed")
    print(f"gate: {state.access_mode.value}; turn: {state.turn_index}")
    agent.require_writable("demo write")
    print("write allowed for the current turn")

    print("\n[3] checkpoint: save memory before emitting the resume token")
    checkpoint = agent.checkpoint(
        reason="token budget",
        summary="read-only probe done",
        next_step="resume and continue",
        decisions=["read-only first"],
    )
    print(checkpoint.text)

    print("\n[4] new agent: resume")
    resumed = LongRunAgent(memory, root=home)
    bundle = resumed.resume()
    print(bundle.summary)
    print(
        f"gate after resume: {bundle.state.access_mode.value}; "
        f"turn: {bundle.state.turn_index}"
    )
    try:
        resumed.require_writable("write after resume")
    except WriteGateViolation as exc:
        print(f"gate: {exc}")
        print("expected: the write lease does not survive resume")

    print("\n[5] re-authorize for the new turn, then finish")
    resumed.enable_writes("re-authorize after resume")
    resumed.require_writable("write after resume")
    done = resumed.finish("demo complete")
    print(f"status: {done.state.status.value}; memory: {done.memory_id}")

    print("\nOK: checkpoint -> resume -> single-turn write lease -> finish")
    print(f"temp project: {home}")
    return 0 if done.state.status is TaskStatus.COMPLETED else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-longrun",
        description="Runtime contracts for long-running LLM agents.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("demo", help="run a self-contained checkpoint/resume demo")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in (None, "demo"):
        try:
            return run_demo()
        except MemoryBackendError as exc:
            print(f"agent-longrun demo failed: {exc}", file=sys.stderr)
            print(
                "Hint: install the router in the same environment "
                "(agent-memory-router), or set AGENT_MEMORY_ROUTER_PATH.",
                file=sys.stderr,
            )
            return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
