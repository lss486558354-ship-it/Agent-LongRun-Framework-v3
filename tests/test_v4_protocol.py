"""v4 提交协议与恢复的 P0 回归测试。

这些测试先在 v3 上失败，驱动 v4 的 checkpoint / pending / resume 改造。
"""

from __future__ import annotations

import pytest

from memory_client import InMemoryBackend, MemoryBackendError
from state_machine import AccessMode, StateStore, TaskStatus
from workflow_manager import (
    CheckpointWriteError,
    LongRunAgent,
    LongRunError,
    WriteGateViolation,
)


class FailingSaveBackend(InMemoryBackend):
    """归档写入失败，模拟 MCP/记忆库不可用。"""

    def save(self, **kwargs):  # type: ignore[override]
        self.calls.append(("save_failed", kwargs.get("title", "")))
        raise MemoryBackendError("模拟归档写入失败")


def start(agent: LongRunAgent) -> None:
    agent.start_task("v4 协议测试任务", keywords="v4,checkpoint")


def test_checkpoint_post_cas_archive_failure_sets_pending(tmp_path) -> None:
    """CAS 已提交但归档失败：state 必须落盘为 pending，且不返回断点。"""
    memory = InMemoryBackend()
    agent = LongRunAgent(memory, root=tmp_path)
    start(agent)

    failing = FailingSaveBackend()
    agent.memory = failing

    with pytest.raises(CheckpointWriteError, match="未输出|归档"):
        agent.checkpoint(reason="token 预警", next_step="继续")

    disk = StateStore(tmp_path).load()
    assert disk is not None
    assert getattr(disk, "archive_pending", False) is True
    assert disk.status is TaskStatus.PAUSED
    assert disk.checkpoint_id is None
    assert (tmp_path / ".longrun" / "checkpoint_journal.json").exists()

    # 查询也必须看到 pending，而不是缓存的旧 in_progress。
    assert agent.status()["archive_pending"] is True


def test_recover_from_memory_refuses_active_task(tmp_path) -> None:
    """本地已有活动任务时，记忆恢复不得直接覆盖它。"""
    memory = InMemoryBackend()
    first = LongRunAgent(memory, root=tmp_path)
    started = first.start_task("活动任务", keywords="v4,active")
    first.checkpoint(reason="中断", next_step="继续")

    second = LongRunAgent(memory, root=tmp_path)
    with pytest.raises(LongRunError, match="活动|未完成|覆盖"):
        second.recover_from_memory(started.state.task_id)

    disk = StateStore(tmp_path).load()
    assert disk is not None
    assert disk.status is TaskStatus.PAUSED
    assert disk.next_step == "继续"


def test_require_writable_stale_lease_persists_revocation(tmp_path) -> None:
    """过期租约被拒绝后，磁盘状态也必须回到 read_only。"""
    memory = InMemoryBackend()
    agent = LongRunAgent(memory, root=tmp_path)
    start(agent)
    agent.enable_writes("方案确认")

    # 模拟外部把轮次推进，但忘了收回 writable 的异常状态。
    state = StateStore(tmp_path).load()
    assert state is not None
    state.turn_index += 1
    StateStore(tmp_path).save(state, expected_revision=state.revision)

    with pytest.raises(WriteGateViolation, match="过期"):
        agent.require_writable("写文件")

    disk = StateStore(tmp_path).load()
    assert disk is not None
    assert disk.access_mode is AccessMode.READ_ONLY
    assert disk.write_lease_turn is None


def test_status_reads_disk_not_cache(tmp_path) -> None:
    """status() 不能让缓存掩盖另一个进程/实例刚写入的状态。"""
    memory = InMemoryBackend()
    agent = LongRunAgent(memory, root=tmp_path)
    start(agent)

    external = StateStore(tmp_path)
    state = external.load()
    assert state is not None
    state.phase = "外部阶段"
    external.save(state, expected_revision=state.revision)

    assert agent.status()["phase"] == "外部阶段"
