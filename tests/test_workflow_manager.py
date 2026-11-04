"""LongRunAgent 的行为测试。

这里守的是三条顺序约束：
1. start_task 先查记忆
2. checkpoint 先写记忆，再返回断点
3. 只读模式不能做写操作

外加断点恢复：本地状态丢失后，必须能从记忆库重建。
"""

from __future__ import annotations

import json
import types

import pytest

import memory_client
from memory_client import InMemoryBackend, MCPMemoryBackend, MemoryBackendError
from state_machine import AccessMode, StateStore, TaskState, TaskStatus
from workflow_manager import (
    CheckpointError,
    CheckpointWriteError,
    LongRunAgent,
    LongRunError,
    NoActiveTask,
    WriteGateViolation,
    extract_keywords,
)


class FailingSaveBackend(InMemoryBackend):
    """模拟记忆服务不可用。"""

    def save(self, **kwargs):  # type: ignore[override]
        self.calls.append(("save_failed", kwargs.get("title", "")))
        raise MemoryBackendError("模拟写入失败")


@pytest.fixture()
def memory() -> InMemoryBackend:
    return InMemoryBackend()


@pytest.fixture()
def agent(tmp_path, memory) -> LongRunAgent:
    return LongRunAgent(memory, root=tmp_path)


def seed(memory: InMemoryBackend, title: str, content: str = "历史正文") -> dict:
    return memory.save(
        type="decision", title=title, content=content, keywords="报价,适配器"
    )


def start(agent: LongRunAgent, **kwargs):
    params = {"keywords": "报价,适配器"}
    params.update(kwargs)
    return agent.start_task("实现报价平台适配器", **params)


# ---------------------------------------------------------------------------
# 约束 1：先查记忆
# ---------------------------------------------------------------------------


def test_start_task_searches_before_creating_state(agent, memory) -> None:
    seed(memory, "报价平台适配器设计")
    memory.calls.clear()

    start(agent)

    assert memory.calls[0][0] == "search"


def test_start_task_hydrates_top_one(agent, memory) -> None:
    seed(memory, "报价平台适配器设计", content="完整方案：走 Playwright")

    result = start(agent)

    assert len(result.memories) == 1
    assert len(result.context) == 1
    assert result.context[0]["content"] == "完整方案：走 Playwright"


def test_start_task_without_history_still_works(agent, memory) -> None:
    result = start(agent)

    assert result.memories == []
    assert result.context == []
    assert "没有命中" in result.summary


def test_start_task_can_skip_hydration(agent, memory) -> None:
    seed(memory, "报价平台适配器设计")

    result = start(agent, hydrate_top=0)

    assert result.memories
    assert result.context == []


def test_start_task_refuses_to_overwrite_active_task(agent) -> None:
    start(agent)

    with pytest.raises(LongRunError, match="已有未完成任务"):
        start(agent)


def test_start_task_defaults_to_read_only(agent) -> None:
    result = start(agent)

    assert result.state.access_mode is AccessMode.READ_ONLY
    assert result.state.turn_index == 1
    assert result.state.write_lease_turn is None
    assert "enable_writes" in result.summary


# ---------------------------------------------------------------------------
# 约束 2：先写记忆再输出断点
# ---------------------------------------------------------------------------


def test_checkpoint_saves_before_returning(agent, memory) -> None:
    start(agent)
    memory.calls.clear()

    checkpoint = agent.checkpoint(reason="token 预警", next_step="接入第二个平台")

    assert [call[0] for call in memory.calls] == ["save"]
    assert checkpoint.state.checkpoint_id == checkpoint.memory_id
    stored = memory.entities[checkpoint.memory_id]
    assert "[断点]" in stored["title"]


def test_checkpoint_pauses_and_persists(agent, tmp_path) -> None:
    start(agent)

    agent.checkpoint(reason="token 预警", next_step="下一步")

    reloaded = StateStore(tmp_path).load()
    assert reloaded is not None
    assert reloaded.status is TaskStatus.PAUSED
    assert reloaded.next_step == "下一步"
    assert reloaded.access_mode is AccessMode.READ_ONLY
    assert reloaded.write_lease_turn is None
    assert reloaded.checkpoint_id


def test_checkpoint_content_contains_machine_marker(agent) -> None:
    start(agent)
    agent.enable_writes("方案确认")

    checkpoint = agent.checkpoint(reason="中断", next_step="继续")

    marker = checkpoint.text.split("<!-- longrun-state: ")[1].split(" -->")[0]
    data = json.loads(marker)
    assert data["task_id"] == checkpoint.state.task_id
    # v3：断点结束本轮执行，写租约在渲染前已被收回。
    assert data["access_mode"] == "read_only"
    assert data["write_lease_turn"] is None
    assert data["turn_index"] == 1
    assert "history" not in data


def test_checkpoint_write_failure_blocks_output(agent, memory, tmp_path) -> None:
    start(agent)
    agent.enable_writes("准备写入")
    failing = FailingSaveBackend()
    failing.entities = memory.entities
    failing.raw = memory.raw
    failing._counters = dict(memory._counters)

    agent.memory = failing

    with pytest.raises(CheckpointWriteError, match="断点未输出"):
        agent.checkpoint(reason="token 预警", next_step="继续")

    # 状态没有被推进，断点文本也没有返回；写权限同样被收回
    assert agent.status()["status"] == "in_progress"
    assert agent.status()["checkpoint_id"] is None
    assert agent.status()["access_mode"] == "read_only"
    assert (tmp_path / ".longrun" / "pending_checkpoint.json").exists()


def test_checkpoint_rejects_unknown_kind(agent) -> None:
    start(agent)

    with pytest.raises(CheckpointError, match="未知断点类型"):
        agent.checkpoint(kind="whatever")


def test_finish_writes_archive_then_terminal(agent, memory) -> None:
    start(agent)

    checkpoint = agent.finish("全部完成")

    assert checkpoint.state.status is TaskStatus.COMPLETED
    assert "[完成]" in memory.entities[checkpoint.memory_id]["title"]
    with pytest.raises(CheckpointError, match="终态"):
        agent.checkpoint()


def test_terminal_task_can_be_replaced(agent) -> None:
    start(agent)
    agent.finish("完成")

    result = start(agent)

    assert result.state.status is TaskStatus.IN_PROGRESS


# ---------------------------------------------------------------------------
# 约束 3：读写门禁
# ---------------------------------------------------------------------------


def test_read_only_blocks_writes(agent) -> None:
    start(agent)

    with pytest.raises(WriteGateViolation, match="只读"):
        agent.require_writable("修改报价适配器")


def test_enable_writes_unlocks(agent) -> None:
    start(agent)

    state = agent.enable_writes("方案已确认")
    agent.require_writable("修改报价适配器")  # 不应抛异常

    assert state.access_mode is AccessMode.WRITABLE
    assert state.write_lease_turn == state.turn_index == 1
    assert "方案已确认" in agent.status()["history"][-1]["reason"]


def test_disable_writes_relocks(agent) -> None:
    start(agent)
    agent.enable_writes("开始实施")
    agent.disable_writes("回到调研")

    assert agent.status()["write_lease_turn"] is None
    with pytest.raises(WriteGateViolation):
        agent.require_writable()


def test_begin_turn_revokes_previous_lease(agent) -> None:
    start(agent)
    agent.enable_writes("方案确认")
    agent.require_writable("第一轮写文件")

    state = agent.begin_turn("用户发来下一条消息")

    assert state.turn_index == 2
    assert state.access_mode is AccessMode.READ_ONLY
    assert state.write_lease_turn is None
    with pytest.raises(WriteGateViolation, match="只读"):
        agent.require_writable("第二轮写文件")

    agent.enable_writes("第二轮方案也确认")
    agent.require_writable("第二轮写文件")
    assert agent.status()["write_lease_turn"] == 2


def test_end_turn_revokes_lease(agent) -> None:
    start(agent)
    agent.enable_writes("方案确认")

    state = agent.end_turn("本轮结束")

    assert state.turn_index == 1
    assert state.access_mode is AccessMode.READ_ONLY
    assert state.write_lease_turn is None
    with pytest.raises(WriteGateViolation):
        agent.require_writable("本轮结束后的写操作")


def test_stale_lease_is_rejected_even_if_mode_is_writable(agent) -> None:
    start(agent)
    agent.enable_writes("方案确认")

    # 模拟外部把状态文件改成 writable、但轮次已经前进的异常情况。
    agent._state.turn_index += 1  # type: ignore[union-attr]

    with pytest.raises(WriteGateViolation, match="已过期"):
        agent.require_writable("写文件")


def test_checkpoint_revokes_lease_before_persisting(agent, tmp_path) -> None:
    start(agent)
    agent.enable_writes("方案确认")

    checkpoint = agent.checkpoint(reason="token 预警", next_step="继续")

    assert checkpoint.state.access_mode is AccessMode.READ_ONLY
    assert checkpoint.state.write_lease_turn is None
    reloaded = StateStore(tmp_path).load()
    assert reloaded is not None
    assert reloaded.access_mode is AccessMode.READ_ONLY
    assert reloaded.write_lease_turn is None


def test_legacy_writable_state_is_revoked_on_load(tmp_path, memory) -> None:
    store = StateStore(tmp_path)
    state = TaskState(
        task_id="T_OLD",
        description="v2 遗留任务",
        access_mode=AccessMode.WRITABLE,
        turn_index=1,
        write_lease_turn=1,
    )
    state.transition(TaskStatus.IN_PROGRESS)
    store.save(state)

    agent = LongRunAgent(memory, root=tmp_path)

    assert agent.status()["access_mode"] == "read_only"
    assert agent.status()["write_lease_turn"] is None
    with pytest.raises(WriteGateViolation):
        agent.require_writable("旧状态直接写")


# ---------------------------------------------------------------------------
# 阶段归档与决策
# ---------------------------------------------------------------------------


def test_complete_phase_keeps_task_running(agent, memory) -> None:
    start(agent)

    checkpoint = agent.complete_phase(
        "阶段1：数据模型", "完成建表与迁移", decisions=["用 SQLite 不引入 ORM"]
    )

    assert checkpoint.state.status is TaskStatus.IN_PROGRESS
    assert checkpoint.state.access_mode is AccessMode.READ_ONLY
    assert checkpoint.state.write_lease_turn is None
    assert "阶段1：数据模型: 完成建表与迁移" in checkpoint.state.completed_steps
    assert "用 SQLite 不引入 ORM" in checkpoint.state.decisions
    assert "[阶段完成]" in memory.entities[checkpoint.memory_id]["title"]


def test_save_decision_uses_decision_type(agent, memory) -> None:
    start(agent)
    checkpoint = agent.checkpoint(reason="中断", next_step="继续")

    record = agent.save_decision(
        "选择 SQLite 而非向量库", "本地零依赖，检索结果可解释", keywords="sqlite"
    )

    assert record["type"] == "decision"
    assert record["keywords"].startswith("longrun,")
    assert checkpoint.state.task_id in record["keywords"]
    assert memory.relations[-1][1] == checkpoint.memory_id


# ---------------------------------------------------------------------------
# 恢复
# ---------------------------------------------------------------------------


def test_resume_restores_paused_task(tmp_path, memory) -> None:
    first = LongRunAgent(memory, root=tmp_path)
    start(first)
    first.enable_writes("方案确认")
    checkpoint = first.checkpoint(reason="token 预警", next_step="接入第二个平台")

    second = LongRunAgent(memory, root=tmp_path)
    bundle = second.resume()

    assert bundle.state.task_id == checkpoint.state.task_id
    assert bundle.state.status is TaskStatus.IN_PROGRESS
    # v3：resume 开启新一轮，上一轮的写租约不继承。
    assert bundle.state.access_mode is AccessMode.READ_ONLY
    assert bundle.state.write_lease_turn is None
    assert bundle.state.turn_index == 2
    assert bundle.state.next_step == "接入第二个平台"
    assert bundle.checkpoint is not None
    assert "[LongRun Checkpoint]" in bundle.checkpoint["content"]

    second.enable_writes("恢复后重新授权")
    second.require_writable("继续修改适配器")
    assert second.status()["write_lease_turn"] == 2


def test_resume_without_state_raises(tmp_path, memory) -> None:
    agent = LongRunAgent(memory, root=tmp_path / "empty")

    with pytest.raises(NoActiveTask, match="没有本地任务状态"):
        agent.resume()


def test_recover_from_memory_rebuilds_state(tmp_path, memory) -> None:
    first = LongRunAgent(memory, root=tmp_path)
    started = start(first)
    checkpoint = first.checkpoint(
        reason="token 预警", next_step="继续实现", decisions=["先做只读探测"]
    )

    # 模拟换机器：状态文件不存在
    fresh = LongRunAgent(memory, root=tmp_path / "fresh")
    bundle = fresh.recover_from_memory(started.state.task_id)

    assert bundle.state.task_id == started.state.task_id
    assert bundle.state.status is TaskStatus.PAUSED
    assert bundle.state.access_mode is AccessMode.READ_ONLY
    assert bundle.state.write_lease_turn is None
    assert bundle.state.turn_index == 2
    assert bundle.state.next_step == "继续实现"
    assert bundle.state.decisions == ["先做只读探测"]
    assert bundle.state.checkpoint_id == checkpoint.memory_id
    assert fresh.status()["task_id"] == started.state.task_id
    with pytest.raises(WriteGateViolation):
        fresh.require_writable("恢复后未授权就写")


def test_recover_from_memory_missing_raises(tmp_path, memory) -> None:
    agent = LongRunAgent(memory, root=tmp_path)

    with pytest.raises(NoActiveTask, match="找不到"):
        agent.recover_from_memory("完全不存在的关键词")


def test_resume_rejects_finished_task(tmp_path, memory) -> None:
    first = LongRunAgent(memory, root=tmp_path)
    start(first)
    first.finish("完成")

    second = LongRunAgent(memory, root=tmp_path)
    with pytest.raises(NoActiveTask, match="已结束"):
        second.resume()


# ---------------------------------------------------------------------------
# 项目绑定
# ---------------------------------------------------------------------------


def test_default_backend_binds_to_project_root(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeBackend:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("workflow_manager.MCPMemoryBackend", FakeBackend)

    LongRunAgent(root=tmp_path)

    assert captured == {"project_path": tmp_path.resolve()}


def test_mcp_backend_passes_project_path(tmp_path) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text("", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    backend = MCPMemoryBackend(server_path=server, project_path=project)

    assert backend._server_args()[1:] == ["--project_path", str(project.resolve())]


def test_mcp_backend_home_is_project_alias(tmp_path) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text("", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()

    backend = MCPMemoryBackend(server_path=server, home=project)

    assert backend.project_path == project.resolve()
    assert backend._server_args()[1] == "--project_path"


def test_mcp_backend_rejects_missing_project(tmp_path) -> None:
    server = tmp_path / "mcp_server.py"
    server.write_text("", encoding="utf-8")

    with pytest.raises(MemoryBackendError, match="项目目录不存在"):
        MCPMemoryBackend(server_path=server, project_path=tmp_path / "nope")


def test_default_router_path_uses_installed_router(monkeypatch, tmp_path) -> None:
    router = tmp_path / "site-packages" / "router"
    router.mkdir(parents=True)
    (router / "mcp_server.py").write_text("", encoding="utf-8")
    (router / "memory_core.py").write_text("", encoding="utf-8")
    spec = types.SimpleNamespace(origin=str(router / "mcp_server.py"))

    monkeypatch.delenv("AGENT_MEMORY_ROUTER_PATH", raising=False)
    monkeypatch.setattr(
        "memory_client.importlib.util.find_spec",
        lambda name: spec if name == "mcp_server" else None,
    )

    assert memory_client._default_router_path() == router.resolve()


def test_default_router_path_falls_back_to_sibling(monkeypatch, tmp_path) -> None:
    package = tmp_path / "site-packages"
    package.mkdir()
    module_file = package / "memory_client.py"
    module_file.write_text("", encoding="utf-8")
    router = tmp_path / "agent-memory-router-v2"
    router.mkdir()
    (router / "mcp_server.py").write_text("", encoding="utf-8")
    (router / "memory_core.py").write_text("", encoding="utf-8")

    monkeypatch.delenv("AGENT_MEMORY_ROUTER_PATH", raising=False)
    monkeypatch.setattr("memory_client.__file__", str(module_file))
    monkeypatch.setattr(
        "memory_client.importlib.util.find_spec", lambda name: None
    )

    assert memory_client._default_router_path() == router.resolve()


def test_default_router_path_env_override(monkeypatch, tmp_path) -> None:
    router = tmp_path / "custom-router"
    router.mkdir()
    (router / "mcp_server.py").write_text("", encoding="utf-8")
    (router / "memory_core.py").write_text("", encoding="utf-8")

    monkeypatch.setenv("AGENT_MEMORY_ROUTER_PATH", str(router))

    assert memory_client._default_router_path() == router.resolve()


# ---------------------------------------------------------------------------
# 关键词提取
# ---------------------------------------------------------------------------


def test_extract_keywords_chinese() -> None:
    keywords = extract_keywords("实现报价平台适配器")

    assert "报价" in keywords
    assert "适配" in keywords


def test_extract_keywords_latin() -> None:
    keywords = extract_keywords("Playwright context isolation fix")

    assert "playwright" in keywords
    assert "context" in keywords
    # 停用词不应出现在结果里
    assert "fix" not in keywords


def test_extract_keywords_empty() -> None:
    assert extract_keywords("") == []
    assert extract_keywords("的了是") == []
