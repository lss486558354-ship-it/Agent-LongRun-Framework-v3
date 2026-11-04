"""CLI smoke test：一条命令必须能跑完断点 -> 恢复 -> 单轮写租约。

用子进程跑，避免 `memory_core` 的模块级全局配置污染测试进程。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_demo_runs_end_to_end() -> None:
    if importlib.util.find_spec("memory_core") is None:
        pytest.skip("agent-memory-router is not installed")

    result = subprocess.run(
        [sys.executable, "-m", "cli", "demo"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "OK: checkpoint -> resume -> single-turn write lease -> finish" in result.stdout
    assert "expected: the write lease does not survive resume" in result.stdout
