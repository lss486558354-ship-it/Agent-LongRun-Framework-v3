"""状态机的行为测试。

核心断言只有一句：**非法流转必须失败**。
如果状态机允许跳跃，那它就只是装饰品。
"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from state_machine import (
    AccessMode,
    StateConflict,
    StateCorruptError,
    StateLockTimeout,
    StateStore,
    TaskState,
    TaskStatus,
    TransitionError,
)


def make_state(**overrides: object) -> TaskState:
    payload = {"task_id": "T_TEST", "description": "测试任务"}
    payload.update(overrides)
    return TaskState(**payload)  # type: ignore[arg-type]


def _bump_state_worker(root: str, start_event) -> None:
    """并发写状态：每轮都在锁内读-改-写，revision 必须严格递增。"""
    store = StateStore(root)
    start_event.wait(10)
    for _ in range(100):
        with store.locked():
            state = store.load()
            if state is None:
                state = make_state()
                state.transition(TaskStatus.IN_PROGRESS, "init")
            store.save(state, expected_revision=state.revision)


def _hold_lock_worker(root: str, ready, release) -> None:
    """子进程持有状态锁，供父进程验证加锁超时。"""
    store = StateStore(root)
    with store.locked():
        ready.set()
        release.wait(10)


def test_pending_to_in_progress() -> None:
    state = make_state()

    state.transition(TaskStatus.IN_PROGRESS, "开始")

    assert state.status is TaskStatus.IN_PROGRESS
    assert state.history[-1]["from"] == "pending"
    assert state.history[-1]["to"] == "in_progress"


def test_pending_to_in_progress_does_not_grant_write() -> None:
    """进入执行态不等于获得写权限，仍需 enable_writes(原因)。"""
    state = make_state()

    state.transition(TaskStatus.IN_PROGRESS, "开始")

    assert state.access_mode is AccessMode.READ_ONLY
    assert state.write_lease_turn is None


def test_invalid_transition_is_rejected() -> None:
    state = make_state()

    # pending 不能直接跳到 completed
    with pytest.raises(TransitionError, match="pending → completed"):
        state.transition(TaskStatus.COMPLETED)


def test_paused_can_resume() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(TaskStatus.PAUSED, "token 预警")

    state.transition(TaskStatus.IN_PROGRESS, "resume")

    assert state.status is TaskStatus.IN_PROGRESS


def test_blocked_can_resume() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(TaskStatus.BLOCKED, "等待账号")

    state.transition(TaskStatus.IN_PROGRESS, "阻塞解除")

    assert state.status is TaskStatus.IN_PROGRESS


@pytest.mark.parametrize("terminal", [TaskStatus.COMPLETED, TaskStatus.FAILED])
def test_terminal_states_have_no_exits(terminal: TaskStatus) -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.transition(terminal)

    with pytest.raises(TransitionError):
        state.transition(TaskStatus.IN_PROGRESS)


def test_history_is_capped() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    for i in range(30):
        state.note(f"事件 {i}")

    assert len(state.history) <= 20


def test_round_trip_preserves_values() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.access_mode = AccessMode.WRITABLE
    state.turn_index = 3
    state.write_lease_turn = 3
    state.completed_steps.append("第一步")
    state.checkpoint_memory_ids.append("S_0001")

    restored = TaskState.from_dict(json.loads(json.dumps(state.to_dict())))

    assert restored.to_dict() == state.to_dict()
    assert restored.access_mode is AccessMode.WRITABLE
    assert restored.turn_index == 3
    assert restored.write_lease_turn == 3


def test_from_dict_defaults_turn_lease_for_legacy_state() -> None:
    restored = TaskState.from_dict({"task_id": "T_OLD", "description": "旧状态"})

    assert restored.turn_index == 0
    assert restored.write_lease_turn is None


def test_transition_revokes_lease() -> None:
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)
    state.access_mode = AccessMode.WRITABLE
    state.turn_index = 1
    state.write_lease_turn = 1

    state.transition(TaskStatus.PAUSED, "人工暂停")

    assert state.access_mode is AccessMode.READ_ONLY
    assert state.write_lease_turn is None


def test_from_dict_normalizes_non_in_progress_lease() -> None:
    restored = TaskState.from_dict(
        {
            "task_id": "T_PAUSED",
            "description": "暂停任务",
            "status": "paused",
            "access_mode": "writable",
            "turn_index": 1,
            "write_lease_turn": 1,
        }
    )

    assert restored.access_mode is AccessMode.READ_ONLY
    assert restored.write_lease_turn is None


def test_from_dict_downgrades_writable_without_lease() -> None:
    restored = TaskState.from_dict(
        {
            "task_id": "T_NO_LEASE",
            "description": "缺少租约的 writable",
            "status": "in_progress",
            "access_mode": "writable",
        }
    )

    assert restored.access_mode is AccessMode.READ_ONLY
    assert restored.write_lease_turn is None


def test_store_save_and_load(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS)

    store.save(state)
    loaded = store.load()

    assert loaded is not None
    assert loaded.task_id == "T_TEST"
    assert loaded.status is TaskStatus.IN_PROGRESS
    assert loaded.revision == 1

    store.save(loaded)
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.revision == 2


def test_store_missing_returns_none(tmp_path) -> None:
    assert StateStore(tmp_path).load() is None


def test_corrupt_state_raises(tmp_path) -> None:
    store = StateStore(tmp_path)
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StateCorruptError, match="状态文件损坏"):
        store.load()


def test_clear_removes_state(tmp_path) -> None:
    store = StateStore(tmp_path)
    state = make_state()
    store.save(state)

    store.clear()

    assert store.load() is None
    # 锁文件不能随状态一起删除：删掉会让并发进程各持不同 inode 的锁。
    assert store.lock_path.exists()


# ---------------------------------------------------------------------------
# PR-2：状态文件并发安全
# ---------------------------------------------------------------------------


def test_state_store_unique_temp_file_per_writer(tmp_path, monkeypatch) -> None:
    """每次 save 必须用自己的临时文件，不能共用 state.json.tmp。"""
    store = StateStore(tmp_path)
    state = make_state()
    names: list[str] = []
    real_replace = os.replace

    def record_replace(src, dst):
        names.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr("state_machine.os.replace", record_replace)

    store.save(state)
    store.save(state)

    assert len(names) == 2
    assert len(set(names)) == 2
    assert all(name.endswith(".json.tmp") for name in names)


def test_state_save_conflict_raises(tmp_path) -> None:
    """基于旧 revision 的写入必须失败，不能覆盖别人的更新。"""
    store = StateStore(tmp_path)
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS, "init")
    store.save(state)
    stale_revision = state.revision

    other = store.load()
    assert other is not None
    other.description = "另一个进程写入的进度"
    store.save(other, expected_revision=other.revision)

    with pytest.raises(StateConflict, match="期望 revision"):
        store.save(state, expected_revision=stale_revision)

    # 冲突失败不能偷偷推进内存里的 revision。
    assert state.revision == stale_revision


def test_non_cooperating_external_write_detected_by_revision(tmp_path) -> None:
    """不持锁直接改 JSON 的外部写入，也要被 revision 检测到。"""
    store = StateStore(tmp_path)
    state = make_state()
    state.transition(TaskStatus.IN_PROGRESS, "init")
    store.save(state)
    base_revision = state.revision

    data = json.loads(store.path.read_text(encoding="utf-8"))
    data["revision"] = base_revision + 10
    data["description"] = "外部直接改文件"
    store.path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(StateConflict, match="期望 revision"):
        store.save(state, expected_revision=base_revision)


def test_state_store_concurrent_writers_do_not_lose_updates(tmp_path) -> None:
    """两个进程各写 100 次，最终 revision 必须正好是 200。"""
    ctx = multiprocessing.get_context("spawn")
    start_event = ctx.Event()
    processes = [
        ctx.Process(target=_bump_state_worker, args=(str(tmp_path), start_event))
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(30)

    assert [process.exitcode for process in processes] == [0, 0]

    final = StateStore(tmp_path).load()
    assert final is not None
    assert final.revision == 200


def test_state_lock_timeout(tmp_path) -> None:
    """锁被另一个进程持有时，必须在超时后报错，而不是无限等待。"""
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(
        target=_hold_lock_worker, args=(str(tmp_path), ready, release)
    )
    holder.start()
    try:
        assert ready.wait(10)
        store = StateStore(tmp_path, lock_timeout=0.2)
        with (
            pytest.raises(StateLockTimeout, match="等待状态文件锁超时"),
            store.locked(),
        ):
            pass
    finally:
        release.set()
        holder.join(10)

    assert holder.exitcode == 0
