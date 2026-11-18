"""Target 2：CAS 失败方不得留下 orphan checkpoint。

v4 的提交协议是：

    state CAS → journal → memory archive

CAS 失败意味着断点没有提交，失败方不能写 memory。这个测试直接替换
失败方的 ``StateStore.save``，强制 CAS 抛 ``StateConflict``，然后检查：

1. 失败方没有产生任何断点归档；
2. 本地 state 丢失后，恢复只会选中真正提交成功的断点。
"""

from __future__ import annotations

from typing import Any

import pytest

from memory_client import InMemoryBackend
from state_machine import StateConflict, StateStore
from workflow_manager import LongRunAgent


def _session_contents(memory: InMemoryBackend) -> list[str]:
    contents: list[str] = []
    for record in memory.entities.values():
        if record.get("type") == "session":
            contents.append(str(memory.read_raw(record["id"])["content"]))
    return contents


def test_cas_conflict_writes_no_archive_and_recovery_uses_committed_checkpoint(
    tmp_path, monkeypatch
) -> None:
    memory = InMemoryBackend()

    winner = LongRunAgent(memory, root=tmp_path)
    started = winner.start_task("Target 2：并发断点", keywords="target2,checkpoint")
    task_id = started.state.task_id

    committed = winner.checkpoint(
        reason="winner",
        next_step="COMMITTED-NEXT-STEP",
    )
    assert committed.memory_id is not None
    assert "COMMITTED-NEXT-STEP" in memory.read_raw(committed.memory_id)["content"]

    # 失败方从 committed state 起步，但强制它的 CAS 失败。
    loser = LongRunAgent(memory, root=tmp_path)
    real_save = loser.store.save

    def conflict_save(state: Any, *, expected_revision: int | None = None, **kwargs: Any) -> None:
        if expected_revision is not None:
            raise StateConflict("模拟 Target 2 的 CAS 冲突")
        real_save(state, expected_revision=expected_revision, **kwargs)

    monkeypatch.setattr(loser.store, "save", conflict_save)

    with pytest.raises(StateConflict, match="Target 2"):
        loser.checkpoint(reason="loser", next_step="ORPHAN-NEXT-STEP")

    # CAS 之前不能写记忆或 journal：失败方没有留下任何 ORPHAN 痕迹。
    assert all("ORPHAN-NEXT-STEP" not in text for text in _session_contents(memory))
    assert not (tmp_path / ".longrun" / "checkpoint_journal.json").exists()

    # 模拟本地 state.json 丢失，只剩记忆库。
    StateStore(tmp_path).clear()

    fresh = LongRunAgent(memory, root=tmp_path)
    bundle = fresh.recover_from_memory(task_id)

    assert bundle.state.checkpoint_id == committed.memory_id
    assert bundle.state.next_step == "COMMITTED-NEXT-STEP"
