"""胶水层：把「状态机」和「记忆路由」串成一个可恢复的长任务 Agent。

核心不是多一个类，而是三条**顺序约束**：

1. `start_task()` 必须先查记忆，再创建任务状态。
   —— 否则 Agent 会在没有历史的情况下重新踩坑。

2. `checkpoint()` 必须先把进度写进记忆库，写入成功后才返回断点文本。
   —— 写入失败就抛异常并落本地待补，绝不"先输出断点、回头再存"。
   顺序反了，一次进程崩溃就会让断点永远消失。

3. 读写门禁默认是 READ_ONLY。
   —— 只有显式 `enable_writes()` 之后才能做写操作。
   调研阶段误改代码，比慢一点危险得多。

4. v3 起，写权限是**单轮租约**，不是持久开关。
   —— `enable_writes()` 只对当前 `turn_index` 有效；
   `begin_turn()` / `end_turn()` / `checkpoint()` / `resume()` /
   `recover_from_memory()` 都会把写权限收回 READ_ONLY。
   一次放行只能执行一轮，下一轮必须重新授权。

用法：

    agent = LongRunAgent(root="D:/my-project")   # 默认 MCP 后端绑定该项目
    start = agent.start_task("实现报价平台适配器", keywords="报价,适配器")
    agent.enable_writes("已确认方案，进入实施")
    ...
    cp = agent.checkpoint(reason="token 预警", next_step="接入第二个平台")
    print(cp.text)                               # 此时记忆已经落库

    # 新进程里恢复（同一个项目根）
    agent2 = LongRunAgent(root="D:/my-project")
    bundle = agent2.resume()
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory_client import (  # noqa: F401 - 重新导出，调用方只需 import 本模块
    InMemoryBackend,
    LocalMemoryBackend,
    MCPMemoryBackend,
    MemoryBackend,
    MemoryBackendError,
)
from state_machine import (
    STATE_SCHEMA_VERSION,
    TERMINAL_STATUSES,
    AccessMode,
    StateConflict,
    StateCorruptError,  # noqa: F401 - 方便调用方捕获
    StateLockTimeout,  # noqa: F401 - 方便调用方捕获
    StatePendingError,
    StateStore,
    TaskState,
    TaskStatus,
    TransitionError,
    now_iso,
)

__all__ = [
    "AccessMode",
    "ActiveTaskExists",
    "AmbiguousRecovery",
    "ArchivePayloadMissing",
    "Checkpoint",
    "CheckpointArchiveError",
    "CheckpointError",
    "CheckpointWriteError",
    "InMemoryBackend",
    "LegacyPendingCheckpoint",
    "LocalMemoryBackend",
    "LongRunAgent",
    "LongRunError",
    "MCPMemoryBackend",
    "MemoryBackendError",
    "NoActiveTask",
    "PendingArchiveError",
    "RecoveryBundle",
    "StateBehindJournal",
    "StateConflict",
    "StateLockTimeout",
    "StatePendingError",
    "TaskStart",
    "TaskState",
    "TaskStatus",
    "TransitionError",
    "WriteGateViolation",
    "extract_keywords",
]


class LongRunError(RuntimeError):
    """本模块所有可预期错误的基类。"""


class NoActiveTask(LongRunError):
    """没有可恢复的任务。"""


class CheckpointError(LongRunError):
    """断点相关错误。"""


class CheckpointWriteError(CheckpointError):
    """记忆写入失败 —— 断点文本被刻意扣住，不返回给调用方。"""


class CheckpointArchiveError(CheckpointWriteError):
    """state 已提交，但 archive 失败。

    这是 v4 的“已提交未归档”错误：本地 state 已经是
    ``archive_pending=True``，但调用方拿不到断点文本；下一次
    ``resume()`` / ``retry_pending_checkpoint()`` 负责补档。
    """

    def __init__(
        self,
        message: str,
        *,
        state: TaskState | None = None,
        checkpoint_key: str | None = None,
        commit_seq: int | None = None,
        journal_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.checkpoint_key = checkpoint_key
        self.commit_seq = commit_seq
        self.journal_path = journal_path


class PendingArchiveError(CheckpointError):
    """state 处于 pending，当前操作必须先完成归档。"""


class ArchivePayloadMissing(CheckpointError):
    """strict_payload=True 时，archive/journal 都没有可用原文。"""


class AmbiguousRecovery(LongRunError):
    """记忆里存在多个无法定序的恢复候选。"""


class ActiveTaskExists(LongRunError):
    """本地已有活动任务，拒绝直接从记忆覆盖。"""


class StateBehindJournal(LongRunError):
    """journal 的 commit_seq 领先 state，不能静默重放。"""


class LegacyPendingCheckpoint(CheckpointError):
    """检测到 v3 pending_checkpoint.json，需要显式处理。"""


class WriteGateViolation(LongRunError):
    """只读模式下尝试写操作。"""


# ---------------------------------------------------------------------------
# 返回结构
# ---------------------------------------------------------------------------


@dataclass
class TaskStart:
    state: TaskState
    memories: list[dict[str, Any]]
    context: list[dict[str, Any]]
    summary: str


@dataclass
class Checkpoint:
    # archive 成功后的 memory record id；CAS 冲突/归档失败时为 None。
    memory_id: str | None
    state: TaskState
    text: str
    kind: str
    checkpoint_key: str | None = None
    commit_seq: int | None = None
    archive_pending: bool = False
    payload_degraded: bool = False


@dataclass
class RecoveryBundle:
    state: TaskState
    checkpoint: dict[str, Any] | None
    related: list[dict[str, Any]]
    summary: str
    checkpoint_key: str | None = None
    commit_seq: int | None = None
    archive_pending: bool = False
    payload_degraded: bool = False
    recovered_from: str = "state"
    unverified: bool = False


# ---------------------------------------------------------------------------
# 关键词提取
# ---------------------------------------------------------------------------

# 这些词单独作为检索词几乎没有区分度
_STOPWORDS = {
    "实现", "修复", "完成", "处理", "支持", "增加", "修改", "优化",
    "一个", "这个", "那个", "如何", "怎么", "以及", "并且", "需要",
    "进行", "我们", "你们", "他们", "可以", "问题", "功能", "任务",
    "the", "and", "for", "with", "fix", "add", "update", "implement",
    "task", "issue", "code", "test",
}

# 含这些字的二字词多半是虚词片段（"断的""被中"），检索价值低
_WEAK_CHARS = set("的了是在和与把对从到为着过")

_CJK_RUN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{1,}|[一-鿿]{2,}")


def extract_keywords(text: str, limit: int = 6) -> list[str]:
    """从任务描述里提取检索词。

    真实项目里更推荐调用方显式传 keywords —— 记忆库是关键词语义，
    不是分词器。这里只是给 `start_task()` 一个不至于空转的兜底。

    中文没有分词，用二字滑窗近似。命中率不如人工关键词，但比整句
    塞进 LIKE 查询（永远匹配不到）强得多。
    """
    candidates: list[str] = []
    for run in _CJK_RUN_RE.findall(text or ""):
        if run[0].isascii():
            token = run.lower()
            if token not in _STOPWORDS:
                candidates.append(token)
            continue

        if len(run) <= 3:
            # 全是"的了是"这类虚词字符的片段没有检索价值
            if run not in _STOPWORDS and not all(ch in _WEAK_CHARS for ch in run):
                candidates.append(run)
            continue

        for i in range(len(run) - 1):
            bigram = run[i : i + 2]
            if bigram in _STOPWORDS:
                continue
            if any(ch in _WEAK_CHARS for ch in bigram):
                continue
            candidates.append(bigram)

    seen: set[str] = set()
    ordered: list[str] = []
    for token in candidates:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(token)
        if len(ordered) >= limit:
            break
    return ordered


def _normalize_keywords(keywords: str | Iterable[str] | None) -> list[str]:
    if keywords is None:
        return []
    if isinstance(keywords, str):
        parts = re.split(r"[,，;；\s]+", keywords)
    else:
        parts = [str(k) for k in keywords]
    return [p.strip() for p in parts if p and p.strip()]


def _short(text: str, width: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 1] + "…"


_STATE_MARKER_RE = re.compile(r"<!--\s*longrun-state:\s*(\{[^\n]*\})\s*-->")


def _sanitize_marker_text(text: str) -> str:
    """避免用户可控文本伪造状态标记。

    真正的 marker 由 `_render_checkpoint()` 单独生成；这里只处理正文里
    的 `<!--` / `-->`，让正文无法闭合或开启一个伪 marker。
    """
    return str(text).replace("<!--", "<! --").replace("-->", "-- >")


def _parse_state_marker(
    content: str,
) -> tuple[TaskState, dict[str, Any]] | None:
    """解析唯一的合法状态标记。

    有 0 个或多个 marker 都视为不可信：旧版取“第一个 marker”的规则
    会让用户正文里的注入串优先于真正的断点。v4 只接受恰好一个。
    """
    matches = list(_STATE_MARKER_RE.finditer(content or ""))
    if len(matches) != 1:
        return None
    try:
        data = json.loads(matches[0].group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "task_id" not in data:
        return None
    try:
        state = TaskState.from_dict(data)
    except (KeyError, TypeError, ValueError, StateCorruptError):
        return None
    return state, data


# ---------------------------------------------------------------------------
# LongRunAgent
# ---------------------------------------------------------------------------


class LongRunAgent:
    """状态机 + 记忆路由的统一门面。"""

    def __init__(
        self,
        memory: MemoryBackend | None = None,
        *,
        root: str | Path | None = None,
        store: StateStore | None = None,
        max_memories: int = 5,
        max_search_terms: int = 4,
        hydrate_top: int = 1,
        strict_memory: bool = True,
        strict_payload: bool = False,
    ) -> None:
        """
        Args:
            memory: 记忆后端。默认 `MCPMemoryBackend(project_path=项目根)`，
                    即真正走 MCP，并绑定到同一个项目。
            root: 项目根目录。状态文件写到 `<root>/.longrun/`，
                  默认记忆库写到 `<root>/memory.db`。默认取环境变量
                  LONGRUN_HOME 或当前工作目录。
            store: 直接传入 StateStore，优先级高于 root。
            max_memories: 单次任务最多带多少条摘要进上下文。
            max_search_terms: 最多用几个关键词去搜（每个关键词一次后端调用）。
            hydrate_top: 自动加载多少条原文。0 表示只给摘要，原文交给 Agent 按需取。
            strict_memory: True 时记忆后端报错直接失败（铁律默认值）；
                           False 时记录为空继续，适合首次搭建记忆库的场景。
            strict_payload: True 时，pending 修复或内存恢复找不到断点原文
                           直接抛 ArchivePayloadMissing；False 时降级重渲染。
        """
        if store is not None:
            project_root = Path(store.root).expanduser().resolve()
            self.store = store
        else:
            project_root = Path(
                root or os.environ.get("LONGRUN_HOME") or Path.cwd()
            ).expanduser().resolve()
            self.store = StateStore(project_root)

        # 状态与默认记忆库必须指向同一个项目，否则 resume 会读错历史。
        self.memory: MemoryBackend = (
            memory if memory is not None else MCPMemoryBackend(project_path=project_root)
        )
        self.max_memories = max(1, int(max_memories))
        self.max_search_terms = max(1, int(max_search_terms))
        self.hydrate_top = max(0, int(hydrate_top))
        self.strict_memory = strict_memory
        self.strict_payload = bool(strict_payload)

        # 启动即加载上一次的状态。状态文件损坏时直接报错 ——
        # 静默当成「没有任务」会让 Agent 覆盖掉真实进度。
        self._state: TaskState | None = self.store.load()

        # v2 的 writable 没有轮次租约。新实例视为新会话：
        # 旧的写权限一律作废，必须重新 enable_writes()。
        # 锁内重新读盘，避免两个新进程同时启动时互相覆盖。
        if self._state and self._state.access_mode is AccessMode.WRITABLE:
            with self.store.locked():
                current = self.store.load()
                if current is not None and current.access_mode is AccessMode.WRITABLE:
                    self._revoke_write_lease(
                        current, "v3: legacy write permission revoked on load"
                    )
                    self.store.save(
                        current,
                        expected_revision=current.revision,
                        expected_task_id=current.task_id,
                        expected_task_epoch=current.task_epoch,
                    )
                    self._state = current
                else:
                    self._state = current

    # -- 任务生命周期 -------------------------------------------------------

    def start_task(
        self,
        task_desc: str,
        *,
        keywords: str | Iterable[str] | None = None,
        hydrate_top: int | None = None,
        task_id: str | None = None,
        **legacy_kwargs: Any,
    ) -> TaskStart:
        """开始长任务。**先检索记忆，再建状态。**

        任务永远以 READ_ONLY 启动：调研阶段不应该改代码。
        确认方案后调用 `enable_writes(原因)` 获取当前轮写租约。

        v3 修复：`access_mode` 不再是公开参数。任何试图在启动时指定写模式的
        旧调用都会直接报错，不存在「启动即写」的合法路径。
        """
        if "access_mode" in legacy_kwargs:
            raise LongRunError(
                "start_task() 不再接受 access_mode：新任务只能以只读启动。"
                "请先完成调研，再调用 enable_writes(原因) 获取当前轮写租约。"
            )
        if legacy_kwargs:
            unexpected = ", ".join(sorted(legacy_kwargs))
            raise TypeError(
                f"start_task() got unexpected keyword argument(s): {unexpected}"
            )

        task_desc = (task_desc or "").strip()
        if not task_desc:
            raise LongRunError("task_desc 不能为空。")

        # 先用磁盘上的权威状态做一次快速检查；检索完再在锁内复查一次。
        # 不依赖 self._state 缓存：另一个进程可能刚结束或刚启动任务。
        with self.store.locked():
            self._ensure_start_allowed(self.store.load())

        # 铁律 1：任务开始先查记忆。
        terms = _normalize_keywords(keywords) or extract_keywords(task_desc)
        memories = self._search_for_task(terms)

        # 只自动加载最相关的少量原文；剩下的留给 Agent 显式 read_raw_data。
        top_n = self.hydrate_top if hydrate_top is None else max(0, int(hydrate_top))
        context = self._hydrate(memories, top_n)

        state = TaskState(
            task_id=task_id or self._new_task_id(),
            description=task_desc,
            access_mode=AccessMode.READ_ONLY,
            turn_index=1,
            schema_version=STATE_SCHEMA_VERSION,
            task_epoch=self._new_task_epoch(),
        )
        state.transition(TaskStatus.IN_PROGRESS, "start_task")

        # 检索可以慢，放在锁外；创建状态必须在锁内重新读盘。
        # 否则两个进程各自看到「没有任务」，后写的那个会覆盖先写的进度。
        with self.store.locked():
            current = self.store.load()
            self._ensure_start_allowed(current)
            expected = current.revision if current is not None else 0
            self.store.save(state, expected_revision=expected)
        self._state = state

        summary = self._start_summary(state, memories, context)
        return TaskStart(state=state, memories=memories, context=context, summary=summary)

    def checkpoint(
        self,
        *,
        reason: str = "",
        summary: str = "",
        next_step: str = "",
        decisions: Iterable[str] | None = None,
        kind: str = "interrupt",
        phase: str | None = None,
        status: TaskStatus | str | None = None,
    ) -> Checkpoint:
        """写断点。**state CAS 是提交点，archive 成功后才返回断点文本。**

        v4 顺序：

            state CAS（archive_pending=True）
              → 写 journal
              → 写 memory archive
              → 清 pending
              → 返回断点

        CAS 失败时不会写 memory，因此不会产生 orphan archive。
        CAS 成功后 archive 失败时，state 已经是 pending，错误会明确告诉
        调用方“断点未输出”，后续用 resume()/retry_pending_checkpoint() 补档。

        Args:
            reason: 为什么触发断点（如「token 预警」「等待用户确认」）。
            summary: 本阶段完成了什么。
            next_step: 下一步做什么。恢复时 Agent 第一眼看的就是它。
            decisions: 本阶段做出的关键决策，会一起写进记忆原文。
            kind: interrupt / phase_complete / blocked / complete。
            phase: 覆盖当前阶段名。
            status: 显式指定目标状态，优先级最高。
        """
        if kind not in {"interrupt", "phase_complete", "blocked", "complete"}:
            raise CheckpointError(f"未知断点类型：{kind}")

        # 第一阶段：读盘 → 副本上改 → 渲染。渲染放在锁内，
        # 保证 marker 的 commit_seq 与将要提交的 revision 一致。
        with self.store.locked():
            state = self.store.load()
            self._ensure_checkpoint_allowed(state)
            assert state is not None  # helper 已保证

            working = TaskState.from_dict(state.to_dict())
            base_revision = state.revision

            if phase:
                working.phase = phase
            if next_step:
                working.next_step = next_step
            if decisions:
                working.decisions.extend(str(d) for d in decisions)

            if summary:
                if kind == "phase_complete":
                    working.completed_steps.append(f"{working.phase}: {summary}")
                else:
                    working.completed_steps.append(summary)

            # 断点即一轮执行的结束。先收回写租约，再渲染状态标记；
            # 这样从断点恢复出来的任务一定是 read_only，必须重新授权。
            self._revoke_write_lease(working, f"checkpoint:{kind}")

            target = self._target_status(kind, status)
            if target is not None and target is not working.status:
                working.transition(target, f"checkpoint:{kind}")

            # v4 协议字段。task_epoch 在第一次 v4 断点时分配给 legacy 任务。
            if working.task_epoch is None:
                working.task_epoch = self._new_task_epoch()
            working.checkpoint_key = secrets.token_hex(16)
            working.commit_seq = base_revision + 1
            working.archive_pending = True
            working.schema_version = STATE_SCHEMA_VERSION
            working.updated_at = now_iso()

            # archive 的 marker 描述“已提交的断点”，因此 archive_pending
            # 归一化为 False；pending 只存在于本地 state。
            archive_state = TaskState.from_dict(working.to_dict())
            archive_state.archive_pending = False
            content = self._render_checkpoint(
                archive_state, kind=kind, reason=reason, summary=summary
            )
            title = self._checkpoint_title(archive_state, kind)
            keywords = self._checkpoint_keywords(archive_state)

        # 第二阶段：CAS 提交。这是唯一的 commit point。
        with self.store.locked():
            self.store.save(
                working,
                expected_revision=base_revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            if working.revision != working.commit_seq:
                # StateStore.save 的契约是 current_revision + 1；不匹配说明
                # 存储层被改写，不能继续归档一个没有提交证明的断点。
                raise CheckpointError(
                    "state revision 与 commit_seq 不一致："
                    f"revision={working.revision}, commit_seq={working.commit_seq}"
                )
            self._state = working

        # CAS 之后才允许写 journal / memory。失败的一方不会走到这里。
        journal_payload = {
            "journal_version": 1,
            "schema_version": STATE_SCHEMA_VERSION,
            "state": working.to_dict(),
            "kind": kind,
            "reason": reason,
            "summary": summary,
            "content": content,
            "archive_title": title,
            "archive_keywords": keywords,
            "checkpoint_key": working.checkpoint_key,
            "task_epoch": working.task_epoch,
            "commit_seq": working.commit_seq,
            "payload_degraded": False,
            "created_at": now_iso(),
        }
        with contextlib.suppress(Exception):
            # journal 是 best-effort 恢复凭证：已提交的 state 仍是权威；
            # archive 失败时还可以从 state 降级渲染。
            self.store.write_journal(journal_payload)

        try:
            record = self._archive_checkpoint(working, title, keywords, content)
        except Exception as exc:  # noqa: BLE001 - 归档失败绝不能放行断点
            self._state = working
            raise CheckpointArchiveError(
                "state 已提交，但归档失败，断点未输出（铁律：先归档再输出）。"
                f"断点已标记 pending（checkpoint_key={working.checkpoint_key}，"
                f"commit_seq={working.commit_seq}）。"
                "恢复记忆服务后调用 resume() 或 retry_pending_checkpoint() 补档。",
                state=working,
                checkpoint_key=working.checkpoint_key,
                commit_seq=working.commit_seq,
                journal_path=self.store.journal_path,
            ) from exc

        record_id = str(record.get("id") or "") if isinstance(record, dict) else ""
        if not record_id:
            raise CheckpointArchiveError(
                "state 已提交，但记忆后端返回的归档记录缺少 id；断点未输出。"
                f"pending 已保留（checkpoint_key={working.checkpoint_key}）。"
                "请用 retry_pending_checkpoint() 重新补档。",
                state=working,
                checkpoint_key=working.checkpoint_key,
                commit_seq=working.commit_seq,
                journal_path=self.store.journal_path,
            )

        # 第三阶段：清 pending。失败时 archive 已经存在，返回 pending=True，
        # 由 resume()/retry 清理；此时断点文本可以安全输出。
        cleared = False
        result_state: TaskState = working
        try:
            with self.store.locked():
                latest = self.store.load()
                if (
                    latest is not None
                    and latest.task_id == working.task_id
                    and latest.task_epoch == working.task_epoch
                    and latest.commit_seq == working.commit_seq
                ):
                    latest.checkpoint_id = record_id
                    if record_id not in latest.checkpoint_memory_ids:
                        latest.checkpoint_memory_ids.append(record_id)
                    latest.archive_pending = False
                    latest.updated_at = now_iso()
                    self.store.save(
                        latest,
                        expected_revision=latest.revision,
                        expected_task_id=latest.task_id,
                        expected_task_epoch=latest.task_epoch,
                    )
                    self._state = latest
                    result_state = latest
                    cleared = True
        except Exception:  # noqa: BLE001 - archive 已成功，包装状态不再阻塞输出
            cleared = False
            self._state = working

        if cleared:
            self.store.remove_journal(
                expected_task_epoch=working.task_epoch,
                expected_commit_seq=working.commit_seq,
            )

        return Checkpoint(
            memory_id=record_id,
            state=result_state,
            text=content,
            kind=kind,
            checkpoint_key=working.checkpoint_key,
            commit_seq=working.commit_seq,
            archive_pending=not cleared,
            payload_degraded=False,
        )

    def complete_phase(
        self,
        phase_name: str,
        summary: str,
        *,
        decisions: Iterable[str] | None = None,
        next_step: str = "",
    ) -> Checkpoint:
        """阶段完成归档。状态保持 in_progress，阶段写入 completed_steps。"""
        return self.checkpoint(
            kind="phase_complete",
            phase=phase_name,
            summary=summary,
            decisions=decisions,
            next_step=next_step,
            reason="阶段完成",
        )

    def finish(self, summary: str, *, next_step: str = "") -> Checkpoint:
        """结束任务。写完结档记忆后进入 completed 终态。"""
        return self.checkpoint(
            kind="complete",
            summary=summary,
            next_step=next_step,
            reason="任务完成",
        )

    def note_blocked(self, reason: str, *, next_step: str = "") -> Checkpoint:
        """外部依赖阻塞时使用。进入 blocked，解除后 resume()。"""
        return self.checkpoint(
            kind="blocked", reason=reason, next_step=next_step, summary=""
        )

    # -- 恢复 ---------------------------------------------------------------

    def resume(
        self,
        *,
        strict_payload: bool | None = None,
        adopt_journal: bool = False,
    ) -> RecoveryBundle:
        """从本地状态恢复，并在 pending 时先补归档。

        v4 顺序：state → journal → memory。state 只能恢复任务状态；
        pending 断点的原文优先从 archive/journal 取，两者都没有时按
        `strict_payload` 决定降级重渲染还是抛 ArchivePayloadMissing。
        """
        strict = self.strict_payload if strict_payload is None else bool(strict_payload)
        recovered_from = "state"

        with self.store.locked():
            state = self.store.load()
            journal = self.store.load_journal()

            if state is None:
                if journal is not None:
                    state = self._state_from_journal(journal)
                    journal_seq = int(journal.get("commit_seq") or 0)
                    self.store.save(
                        state,
                        expected_revision=0,
                        minimum_revision=journal_seq + 1,
                    )
                    reloaded = self.store.load()
                    if reloaded is None:
                        raise StateCorruptError(
                            "journal 重建 state 后无法重新读取 state.json。"
                        )
                    state = reloaded
                    recovered_from = "journal"
                    # journal 继续留给 _repair_pending：state 只能恢复任务
                    # 状态，断点原文仍在 journal 里。
                elif self.store.legacy_fallback_path.exists():
                    raise LegacyPendingCheckpoint(
                        "检测到 v3 的 pending_checkpoint.json。"
                        "这是旧版断点失败留下的 pending 文件；"
                        "请显式调用 retry_legacy_checkpoint() 归档，或 "
                        "clear(force=True) 丢弃。"
                    )
                else:
                    raise NoActiveTask(
                        "没有本地任务状态。若换过机器/删过 .longrun，"
                        "请用 recover_from_memory(关键词或任务ID) 从记忆库重建。"
                    )
            else:
                if journal is not None:
                    journal_seq = journal.get("commit_seq")
                    if (
                        journal_seq is not None
                        and int(journal_seq) > state.revision
                    ):
                        if not adopt_journal:
                            raise StateBehindJournal(
                                f"journal commit_seq={journal_seq} 领先 "
                                f"state revision={state.revision}。"
                                "默认不重放；确认 journal 可信后传 "
                                "adopt_journal=True。"
                            )
                        adopted = self._state_from_journal(journal)
                        self.store.save(
                            adopted,
                            expected_revision=state.revision,
                            expected_task_id=state.task_id,
                            expected_task_epoch=state.task_epoch,
                            minimum_revision=int(journal_seq) + 1,
                        )
                        state = self.store.load() or adopted
                        recovered_from = "journal"
                        # 同上：保留 journal 作为 pending 的原文来源。
                    elif not state.archive_pending:
                        # journal 不领先且 state 没有 pending：这是上一轮
                        # 归档成功后没删干净的 stale journal，可以安全删除。
                        self.store.remove_journal()
                        journal = None

                if not state.archive_pending and self.store.legacy_fallback_path.exists():
                    raise LegacyPendingCheckpoint(
                        "检测到 v3 的 pending_checkpoint.json，且当前 state "
                        "不是 v4 pending。请显式调用 retry_legacy_checkpoint() "
                        "或 clear(force=True)。"
                    )

                if state.status in TERMINAL_STATUSES and not state.archive_pending:
                    raise NoActiveTask(
                        f"任务 {state.task_id} 已结束（{state.status.value}），"
                        "没有可恢复的进度。"
                    )

            pending = state.archive_pending

        checkpoint_payload: dict[str, Any] | None = None
        payload_degraded = False

        if pending:
            state, checkpoint_payload, payload_degraded, repair_source = (
                self._repair_pending(state, journal=journal, strict_payload=strict)
            )
            if repair_source != recovered_from:
                recovered_from = f"{recovered_from}+{repair_source}"
            if state.status in TERMINAL_STATUSES:
                raise NoActiveTask(
                    f"任务 {state.task_id} 已结束（{state.status.value}），"
                    "断点已补归档，没有可继续的进度。"
                )

        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("恢复期间本地状态被删除，无法继续。")
            if state.archive_pending:
                raise PendingArchiveError(
                    "状态仍有待归档断点；先完成 retry_pending_checkpoint()。"
                )
            if state.status in TERMINAL_STATUSES:
                raise NoActiveTask(
                    f"任务 {state.task_id} 已结束（{state.status.value}），"
                    "没有可恢复的进度。"
                )

            working = TaskState.from_dict(state.to_dict())
            if working.status in {
                TaskStatus.PENDING,
                TaskStatus.PAUSED,
                TaskStatus.BLOCKED,
                TaskStatus.WAITING_VERIFICATION,
            }:
                working.transition(TaskStatus.IN_PROGRESS, "resume")

            # resume 是新的对话轮次：上一轮的写租约不能继承。
            self._start_new_turn(working, "resume")
            self.store.save(
                working,
                expected_revision=state.revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            self._state = working

        # 断点原文与关联检索都是只读操作，放在锁外，避免慢 I/O 阻塞其它进程。
        if checkpoint_payload is None:
            missing_error: MemoryBackendError | None = None
            if working.checkpoint_id:
                try:
                    checkpoint_payload = self.memory.read_raw(working.checkpoint_id)
                except MemoryBackendError as exc:
                    missing_error = exc
            # checkpoint_id 只是 best-effort 缓存；它失效时按权威的
            # checkpoint_key 再找一次 archive。
            if checkpoint_payload is None and working.checkpoint_key:
                found, index_found = self._search_archive(working.checkpoint_key, working)
                if found is not None:
                    checkpoint_payload = found
                elif strict and index_found:
                    raise ArchivePayloadMissing(
                        "索引里有 checkpoint_key，但原文不可读："
                        f"{working.checkpoint_key}"
                    )
                elif index_found:
                    payload_degraded = True
            if checkpoint_payload is None and missing_error is not None:
                if strict:
                    raise ArchivePayloadMissing(
                        f"断点 archive {working.checkpoint_id} 的原文不可读，"
                        "且 checkpoint_key 也没有可用副本。"
                    ) from missing_error
                payload_degraded = True

        related = self._safe_search(working.task_id)
        summary = (
            f"已恢复任务 {working.task_id}（{working.status.value}）。"
            f"阶段：{working.phase}；下一步：{working.next_step or '未指定'}。"
            f"来源：{recovered_from}；"
            f"commit_seq={working.commit_seq}；"
            f"pending={'否' if not working.archive_pending else '是'}；"
            f"payload_degraded={'是' if payload_degraded else '否'}。"
        )
        return RecoveryBundle(
            state=working,
            checkpoint=checkpoint_payload,
            related=related,
            summary=summary,
            checkpoint_key=working.checkpoint_key,
            commit_seq=working.commit_seq,
            archive_pending=working.archive_pending,
            payload_degraded=payload_degraded,
            recovered_from=recovered_from,
            unverified=False,
        )

    def retry_pending_checkpoint(
        self, *, strict_payload: bool | None = None
    ) -> RecoveryBundle:
        """只补归档并清 pending；不改变任务状态和对话轮次。"""
        strict = self.strict_payload if strict_payload is None else bool(strict_payload)
        with self.store.locked():
            state = self.store.load()
            journal = self.store.load_journal()
            if state is None:
                raise NoActiveTask("当前没有活动任务，无法补归档。")
            if not state.archive_pending:
                raise PendingArchiveError("当前没有待归档断点。")

        state, payload, degraded, source = self._repair_pending(
            state, journal=journal, strict_payload=strict
        )
        related = self._safe_search(state.task_id)
        summary = (
            f"已补归档任务 {state.task_id}。来源：{source}；"
            f"checkpoint_key={state.checkpoint_key}；"
            f"payload_degraded={'是' if degraded else '否'}。"
        )
        return RecoveryBundle(
            state=state,
            checkpoint=payload,
            related=related,
            summary=summary,
            checkpoint_key=state.checkpoint_key,
            commit_seq=state.commit_seq,
            archive_pending=False,
            payload_degraded=degraded,
            recovered_from=source,
            unverified=False,
        )

    def retry_legacy_checkpoint(self) -> dict[str, Any]:
        """显式处理 v3 的 pending_checkpoint.json：重渲染并归档，然后删除。"""
        fallback = self.store.legacy_fallback_path
        if not fallback.exists():
            raise LegacyPendingCheckpoint("没有 v3 pending_checkpoint.json 可处理。")
        try:
            data = json.loads(fallback.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptError(
                f"v3 pending checkpoint 损坏：{fallback}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("state"), dict):
            raise LegacyPendingCheckpoint("v3 pending checkpoint 缺少 state 字段。")

        state = TaskState.from_dict(data["state"])
        state.archive_pending = False
        state.schema_version = STATE_SCHEMA_VERSION
        if state.task_epoch is None:
            state.task_epoch = self._new_task_epoch()
        if state.checkpoint_key is None:
            state.checkpoint_key = secrets.token_hex(16)
        if state.commit_seq is None:
            state.commit_seq = state.revision + 1
        kind = str(data.get("kind") or "interrupt")
        content = self._render_checkpoint(
            state,
            kind=kind,
            reason=str(data.get("reason") or ""),
            summary=str(data.get("summary") or ""),
        )
        title = self._checkpoint_title(state, kind)
        keywords = self._checkpoint_keywords(state)
        record = self.memory.save(
            type="session", title=title, content=content, keywords=keywords
        )
        with self.store.locked():
            fallback.unlink(missing_ok=True)
        return record

    def clear(self, *, force: bool = False) -> None:
        """清除本地状态；pending 时默认拒绝。"""
        with self.store.locked():
            state = self.store.load()
            if state is not None and state.archive_pending and not force:
                raise PendingArchiveError(
                    f"任务 {state.task_id} 还有待归档断点，拒绝 clear()。"
                    "先 retry_pending_checkpoint()；确需丢弃请用 force=True。"
                )
            if self.store.legacy_fallback_path.exists() and not force:
                raise LegacyPendingCheckpoint(
                    "检测到 v3 pending_checkpoint.json；"
                    "先 retry_legacy_checkpoint()，或 clear(force=True) 丢弃。"
                )
            self.store.clear(force=True)
        self._state = None

    def recover_from_memory(
        self,
        keyword: str,
        *,
        limit: int = 20,
        task_id: str | None = None,
        checkpoint_key: str | None = None,
        allow_legacy: bool = False,
        include_terminal: bool = False,
        force: bool = False,
        on_ambiguous: str = "raise",
    ) -> RecoveryBundle:
        """本地状态丢失时，从记忆库里的断点标记重建状态。

        v4 不再“取第一条”。候选按 task_id / task_epoch / checkpoint_key /
        commit_seq 过滤；同一任务取最高 commit_seq，不同 epoch 才判歧义。
        旧标记默认 unverified，需要 allow_legacy=True 才参与。
        """
        if on_ambiguous not in {"raise", "latest"}:
            raise LongRunError("on_ambiguous 只能是 'raise' 或 'latest'。")

        search_term = checkpoint_key or task_id or keyword or "longrun"
        rows = self.memory.search(search_term, limit=limit)
        candidates: list[dict[str, Any]] = []
        raw_missing = 0
        legacy_seen = 0

        for row in rows:
            if row.get("type") != "session":
                continue
            record_id = str(row.get("id", ""))
            if not record_id:
                continue
            try:
                payload = self.memory.read_raw(record_id)
            except MemoryBackendError:
                raw_missing += 1
                continue
            parsed = _parse_state_marker(str(payload.get("content", "")))
            if parsed is None:
                continue
            state, _data = parsed
            key = state.checkpoint_key
            epoch = state.task_epoch
            seq = state.commit_seq
            haystack = " ".join(
                str(row.get(field, ""))
                for field in ("id", "title", "keywords")
            )
            verified = bool(key and epoch and seq is not None and key in haystack)
            legacy = not verified

            if task_id is not None and state.task_id != task_id:
                continue
            if checkpoint_key is not None and key != checkpoint_key:
                continue
            if state.status in TERMINAL_STATUSES and not include_terminal:
                continue
            if legacy and not allow_legacy:
                legacy_seen += 1
                continue

            candidates.append(
                {
                    "record_id": record_id,
                    "row": row,
                    "payload": payload,
                    "state": state,
                    "key": key,
                    "epoch": epoch,
                    "commit_seq": seq,
                    "verified": verified,
                    "legacy": legacy,
                }
            )

        if not candidates:
            if raw_missing:
                raise ArchivePayloadMissing(
                    "记忆索引里有候选记录，但 read_raw 读不到原文；"
                    "不能从自然语言猜状态。请检查 raw_data 或使用 resume()。"
                )
            if legacy_seen:
                raise NoActiveTask(
                    "只找到 legacy 断点（缺少 task_epoch/checkpoint_key/"
                    "commit_seq）。确认来源可信后传 allow_legacy=True。"
                )
            raise NoActiveTask(
                f"在记忆库里找不到带状态标记的断点（检索词：{search_term!r}）。"
                "请确认任务开始时用的是同一个记忆库。"
            )

        # verified 优先；只有没有 verified 候选时才考虑 legacy。
        verified_candidates = [c for c in candidates if c["verified"]]
        pool = verified_candidates or candidates

        task_ids = {c["state"].task_id for c in pool}
        if len(task_ids) > 1:
            if on_ambiguous != "latest":
                raise AmbiguousRecovery(
                    "记忆里存在多个任务的断点："
                    + ", ".join(sorted(task_ids))
                    + "。请传 task_id 精确指定，或 on_ambiguous='latest'。"
                )
            chosen_task = max(
                task_ids,
                key=lambda tid: max(
                    (c["commit_seq"] or 0)
                    for c in pool
                    if c["state"].task_id == tid
                ),
            )
            pool = [c for c in pool if c["state"].task_id == chosen_task]

        epochs = {c["epoch"] for c in pool}
        if len(epochs) > 1:
            if on_ambiguous != "latest":
                raise AmbiguousRecovery(
                    f"任务 {pool[0]['state'].task_id} 存在多个 task_epoch："
                    + ", ".join(sorted(str(e) for e in epochs))
                    + "。请传 checkpoint_key 精确指定，或 on_ambiguous='latest'。"
                )
            chosen_epoch = max(
                epochs, key=lambda ep: max(
                    (c["commit_seq"] or 0) for c in pool if c["epoch"] == ep
                )
            )
            pool = [c for c in pool if c["epoch"] == chosen_epoch]

        chosen = max(
            pool,
            key=lambda c: (
                c["commit_seq"] or 0,
                str(c["row"].get("created_at", "")),
                c["record_id"],
            ),
        )

        chosen_state = TaskState.from_dict(chosen["state"].to_dict())
        chosen_state.checkpoint_id = chosen["record_id"]
        chosen_state.checkpoint_key = chosen["key"]
        chosen_state.task_epoch = chosen["epoch"]
        chosen_state.commit_seq = chosen["commit_seq"]
        chosen_state.archive_pending = False
        chosen_state.schema_version = STATE_SCHEMA_VERSION
        if chosen["record_id"] not in chosen_state.checkpoint_memory_ids:
            chosen_state.checkpoint_memory_ids.append(chosen["record_id"])
        chosen_state.history.append(
            {
                "from": "memory",
                "to": chosen_state.status.value,
                "reason": f"recover_from_memory:{chosen['record_id']}",
                "at": now_iso(),
            }
        )
        # 从记忆重建也是新的对话轮次：旧标记里的 writable 一律作废。
        self._start_new_turn(chosen_state, "recover_from_memory")

        with self.store.locked():
            current = self.store.load()
            self._ensure_journal_not_ahead(current)
            self._ensure_recover_allowed(
                current,
                chosen_state,
                force=force,
                include_terminal=include_terminal,
            )
            expected = current.revision if current is not None else 0
            self.store.save(
                chosen_state,
                expected_revision=expected,
                expected_task_id=current.task_id if current is not None else None,
                expected_task_epoch=(
                    current.task_epoch if current is not None else None
                ),
            )
        self._state = chosen_state

        related = [r for r in rows if r.get("id") != chosen["record_id"]]
        summary = (
            f"已从记忆 {chosen['record_id']} 重建任务 "
            f"{chosen_state.task_id}。"
            f"状态：{chosen_state.status.value}；"
            f"下一步：{chosen_state.next_step or '未指定'}；"
            f"commit_seq={chosen_state.commit_seq}；"
            f"unverified={'是' if chosen['legacy'] else '否'}。"
        )
        return RecoveryBundle(
            state=chosen_state,
            checkpoint=chosen["payload"],
            related=related,
            summary=summary,
            checkpoint_key=chosen_state.checkpoint_key,
            commit_seq=chosen_state.commit_seq,
            archive_pending=False,
            payload_degraded=False,
            recovered_from="memory",
            unverified=chosen["legacy"],
        )

    # -- 读写门禁 -----------------------------------------------------------

    def begin_turn(self, reason: str = "") -> TaskState:
        """开始新的一轮对话。上一轮的写租约自动失效。

        宿主每收到一条新的用户消息时调用一次；`resume()` 已经相当于
        开始了一轮，不要对同一条消息重复调用。
        """
        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
            if state.status in TERMINAL_STATUSES:
                raise LongRunError("任务已结束，不能开始新的对话轮次。")
            self._start_new_turn(state, reason or "begin_turn")
            self.store.save(
                state,
                expected_revision=state.revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            self._state = state
            return state

    def end_turn(self, reason: str = "") -> TaskState:
        """结束当前对话轮次。写权限立即收回，下一轮必须重新授权。"""
        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
            if state.status in TERMINAL_STATUSES:
                raise LongRunError("任务已结束，不能结束对话轮次。")
            self._revoke_write_lease(state, reason or "end_turn")
            state.note("turn end" + (f"（{reason}）" if reason else ""))
            self.store.save(
                state,
                expected_revision=state.revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            self._state = state
            return state

    def enable_writes(self, reason: str = "") -> TaskState:
        """在当前对话轮次内解锁写操作。

        v3 的写权限是单轮租约：只对 `state.turn_index` 有效。
        开始下一轮、checkpoint、resume 都会自动收回。

        只有 `in_progress` 状态可以解锁；paused / blocked /
        waiting_verification / 终态都不允许写。`reason` 必须非空，用于审计。
        """
        reason = (reason or "").strip()
        if not reason:
            raise LongRunError(
                "enable_writes() 必须说明原因：写权限变更需要可审计的授权理由。"
            )
        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
            if state.archive_pending:
                raise PendingArchiveError(
                    f"任务 {state.task_id} 还有待归档断点 "
                    f"(checkpoint_key={state.checkpoint_key!r})；"
                    "先 resume() 或 retry_pending_checkpoint()，再解锁写权限。"
                )
            if state.status is not TaskStatus.IN_PROGRESS:
                raise LongRunError(
                    f"当前状态 {state.status.value} 不允许解锁写权限；"
                    "只有 in_progress 可以写。请先 resume() 回到执行态。"
                )
            if state.turn_index <= 0:
                # 兼容手工构造的旧状态：至少给它一个当前轮次。
                state.turn_index = 1
            previous = state.access_mode
            state.access_mode = AccessMode.WRITABLE
            state.write_lease_turn = state.turn_index
            state.note(
                f"access_mode: {previous.value} → writable "
                f"(turn {state.turn_index})"
                + (f"（{reason}）" if reason else "")
            )
            self.store.save(
                state,
                expected_revision=state.revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            self._state = state
            return state

    def disable_writes(self, reason: str = "") -> TaskState:
        """手动回到对话态，并收回本轮写租约。"""
        return self._set_access_mode(AccessMode.READ_ONLY, reason)

    def require_writable(self, action: str = "写操作") -> None:
        """执行任何修改代码/文件的操作前调用。"""
        # 门禁必须读磁盘上的最新状态：另一个进程可能刚把任务 pause 或
        # 收回了租约，内存缓存不能作为放行依据。
        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
            self._state = state
            if state.archive_pending:
                raise WriteGateViolation(
                    f"「{action}」被拒绝：任务 {state.task_id} 还有待归档断点。"
                    "先 resume() 或 retry_pending_checkpoint() 完成归档。"
                )
            if state.status is not TaskStatus.IN_PROGRESS:
                raise WriteGateViolation(
                    f"「{action}」被拒绝：当前状态 {state.status.value} 不允许写操作。"
                    "只有 in_progress 可以写；请先 resume() 并重新 enable_writes(原因)。"
                )
            if state.access_mode is not AccessMode.WRITABLE:
                raise WriteGateViolation(
                    f"当前是只读/对话态，拒绝执行「{action}」。"
                    "先完成调研并调用 enable_writes(原因) 显式解锁。"
                )
            if state.write_lease_turn != state.turn_index:
                leased_turn = state.write_lease_turn
                # 手工改过状态 / 旧版本遗留：宁可报错，也不放行过期租约。
                # v4：收回必须落盘，否则磁盘仍是 writable，门禁形同虚设。
                self._revoke_write_lease(state, "stale write lease")
                try:
                    self.store.save(
                        state,
                        expected_revision=state.revision,
                        expected_task_id=state.task_id,
                        expected_task_epoch=state.task_epoch,
                    )
                except Exception as exc:  # noqa: BLE001 - 拒绝写入优先于持久化
                    raise WriteGateViolation(
                        f"「{action}」被拒绝：写权限已过期，但收回租约时"
                        "状态文件未能更新。请检查锁/磁盘后重新 resume()。"
                    ) from exc
                self._state = state
                raise WriteGateViolation(
                    f"「{action}」被拒绝：写权限已过期"
                    f"（授权轮次 {leased_turn}，当前轮次 {state.turn_index}）。"
                    "已收回租约；请在当前轮次重新调用 enable_writes(原因)。"
                )

    def _set_access_mode(self, mode: AccessMode, reason: str) -> TaskState:
        with self.store.locked():
            state = self.store.load()
            if state is None:
                raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
            if state.status in TERMINAL_STATUSES:
                raise LongRunError("任务已结束，不能再切换读写门禁。")
            previous = state.access_mode
            state.access_mode = mode
            if mode is AccessMode.READ_ONLY:
                state.write_lease_turn = None
            state.note(
                f"access_mode: {previous.value} → {mode.value}"
                + (f"（{reason}）" if reason else "")
            )
            self.store.save(
                state,
                expected_revision=state.revision,
                expected_task_id=state.task_id,
                expected_task_epoch=state.task_epoch,
            )
            self._state = state
            return state

    # -- 决策记录 -----------------------------------------------------------

    def save_decision(
        self,
        title: str,
        content: str,
        *,
        keywords: str = "",
    ) -> dict[str, Any]:
        """单独沉淀一条重大决策（type=decision）。

        断点记录的是「进度」，决策记录的是「为什么」。两者分开存，
        以后检索「当时为什么选 A」时才能精准命中。
        """
        state = self.store.load()
        if state is not None and state.archive_pending:
            raise PendingArchiveError(
                f"任务 {state.task_id} 还有待归档断点；"
                "先 resume()/retry_pending_checkpoint()，再写决策记录。"
            )
        task_id = state.task_id if state else ""
        keyword_text = ",".join(
            p for p in ("longrun", task_id, keywords) if p
        )
        relates_to = ""
        if state is not None:
            record_id = state.checkpoint_id
            if not record_id and state.checkpoint_key:
                # checkpoint_id 只是 archive 后的 best-effort 缓存；
                # 按 checkpoint_key 找回真正的 record id。
                payload, _ = self._search_archive(state.checkpoint_key, state)
                if payload is not None:
                    record_id = str(payload.get("id") or "")
            if record_id:
                relates_to = f"{record_id}:related_to"
        return self.memory.save(
            type="decision",
            title=title,
            content=content,
            keywords=keyword_text,
            relates_to=relates_to,
        )

    # -- 查询 ---------------------------------------------------------------

    def status(self) -> dict[str, Any] | None:
        # 门禁读磁盘，查询也必须读磁盘；缓存只用于减少重复解析，
        # 不能成为另一个进程已修改状态的“遮罩”。
        state = self.store.load()
        self._state = state
        return state.to_dict() if state else None

    # -- 内部 ---------------------------------------------------------------

    @staticmethod
    def _ensure_no_active_task(state: TaskState | None) -> None:
        """磁盘上已有未完成任务时拒绝覆盖；终态任务允许被新任务替换。"""
        if state is not None and state.status not in TERMINAL_STATUSES:
            raise LongRunError(
                f"已有未完成任务 {state.task_id}（{state.status.value}）。"
                "请先 resume() 继续，或 finish() 结束，不要直接覆盖。"
            )

    def _ensure_journal_not_ahead(self, state: TaskState | None) -> None:
        """journal 不能领先 state；只有同一任务的 stale journal 才可清理。"""
        journal = self.store.load_journal()
        if journal is None:
            return
        if state is None:
            raise StateBehindJournal(
                "存在 checkpoint journal，但 state.json 不存在。"
                "请先 resume() 用 journal 重建，不要直接 start_task() 覆盖。"
            )
        try:
            seq = int(journal.get("commit_seq"))
        except (TypeError, ValueError) as exc:
            raise StateCorruptError(
                "checkpoint journal 的 commit_seq 不是整数，无法对账。"
            ) from exc
        if seq > state.revision:
            raise StateBehindJournal(
                f"journal commit_seq={seq} 领先 state revision="
                f"{state.revision}；请先 resume(adopt_journal=True) 对账。"
            )
        if not state.archive_pending:
            journal_state = journal.get("state")
            journal_task_id = (
                journal_state.get("task_id")
                if isinstance(journal_state, dict)
                else None
            )
            journal_epoch = journal.get("task_epoch")
            if (
                journal_task_id != state.task_id
                or journal_epoch != state.task_epoch
            ):
                raise StateBehindJournal(
                    "checkpoint journal 属于另一个任务/世代 "
                    f"(task_id={journal_task_id!r}, "
                    f"task_epoch={journal_epoch!r})；"
                    "先 resume() 对账，或 clear(force=True) 明确丢弃。"
                )
            # 同一任务、不领先且没有 pending：归档已完成但没删干净，安全删除。
            self.store.remove_journal(
                expected_task_epoch=state.task_epoch,
                expected_commit_seq=seq,
            )

    def _ensure_start_allowed(self, state: TaskState | None) -> None:
        if state is not None and state.archive_pending:
            raise PendingArchiveError(
                f"任务 {state.task_id} 还有待归档断点；"
                "先 resume()/retry_pending_checkpoint()，再 start_task()。"
            )
        self._ensure_journal_not_ahead(state)
        self._ensure_no_active_task(state)
        if self.store.legacy_fallback_path.exists():
            raise LegacyPendingCheckpoint(
                "检测到 v3 pending_checkpoint.json；"
                "先 retry_legacy_checkpoint()，或 clear(force=True) 丢弃。"
            )

    def _ensure_checkpoint_allowed(self, state: TaskState | None) -> None:
        if state is None:
            raise NoActiveTask("当前没有活动任务，先调用 start_task()。")
        if state.archive_pending:
            raise PendingArchiveError(
                f"任务 {state.task_id} 还有待归档断点 "
                f"(checkpoint_key={state.checkpoint_key!r})；"
                "先 resume()/retry_pending_checkpoint()，再写新断点。"
            )
        if state.status in TERMINAL_STATUSES:
            raise CheckpointError(
                f"任务 {state.task_id} 已处于终态 {state.status.value}，"
                "不能再写断点。"
            )
        self._ensure_journal_not_ahead(state)
        if self.store.legacy_fallback_path.exists():
            raise LegacyPendingCheckpoint(
                "检测到 v3 pending_checkpoint.json；"
                "先 retry_legacy_checkpoint()，或 clear(force=True) 丢弃。"
            )

    @staticmethod
    def _ensure_recover_allowed(
        current: TaskState | None,
        candidate: TaskState,
        *,
        force: bool,
        include_terminal: bool,
    ) -> None:
        """保护本地活动任务，禁止记忆恢复无条件覆盖。"""
        if candidate.status in TERMINAL_STATUSES and not include_terminal:
            raise ActiveTaskExists(
                "候选断点是终态；默认不用于覆盖当前任务。"
                "确需恢复终态请传 include_terminal=True。"
            )
        if current is None:
            return
        if current.archive_pending:
            raise ActiveTaskExists(
                f"本地任务 {current.task_id} 还有待归档断点；"
                "先 resume()/retry_pending_checkpoint()，不要直接从记忆覆盖。"
            )
        same_task = current.task_id == candidate.task_id
        if current.status not in TERMINAL_STATUSES:
            if not force or not same_task:
                raise ActiveTaskExists(
                    f"本地已有活动任务 {current.task_id}"
                    f"（{current.status.value}）；拒绝从记忆覆盖。"
                    "先 finish()/clear()，或用 force=True 覆盖同一任务。"
                )
            return
        # 本地是终态：默认也拒绝，避免误把旧进度拉回来。
        if not force:
            raise ActiveTaskExists(
                f"本地任务 {current.task_id} 已结束（{current.status.value}）；"
                "确需从记忆重建同一任务请传 force=True。"
            )

    @staticmethod
    def _state_from_journal(journal: dict[str, Any]) -> TaskState:
        data = journal.get("state")
        if not isinstance(data, dict):
            raise StateCorruptError(
                "checkpoint journal 缺少 state 对象，无法重建任务状态。"
            )
        state = TaskState.from_dict(data)
        # journal 只存在于“已提交、待归档”阶段。
        state.archive_pending = True
        return state

    def _search_archive(
        self, key: str, state: TaskState
    ) -> tuple[dict[str, Any] | None, bool]:
        """按 checkpoint_key 搜索 archive。

        Returns:
            (payload, index_found)
            - payload 非空：找到并校验通过；
            - index_found=True 但 payload=None：索引命中但 raw 不可读或
              marker 不匹配；调用方要按 strict_payload 决定降级/报错。
        """
        if not key:
            return None, False
        try:
            rows = self.memory.search(key, limit=20)
        except MemoryBackendError:
            return None, False

        index_found = False
        for row in rows:
            if row.get("type") != "session":
                continue
            record_id = str(row.get("id", ""))
            if not record_id:
                continue
            haystack = " ".join(
                str(row.get(field, ""))
                for field in ("id", "title", "keywords")
            )
            if key not in haystack:
                continue
            index_found = True
            try:
                payload = self.memory.read_raw(record_id)
            except MemoryBackendError:
                continue
            parsed = _parse_state_marker(str(payload.get("content", "")))
            if parsed is None:
                continue
            candidate, _data = parsed
            if candidate.task_id != state.task_id:
                continue
            if candidate.checkpoint_key != key:
                continue
            if (
                state.task_epoch is not None
                and candidate.task_epoch != state.task_epoch
            ):
                continue
            if (
                state.commit_seq is not None
                and candidate.commit_seq != state.commit_seq
            ):
                continue
            return payload, True
        return None, index_found

    def _archive_checkpoint(
        self,
        state: TaskState,
        title: str,
        keywords: str,
        content: str,
    ) -> dict[str, Any]:
        """归档断点；同一 checkpoint_key 已存在时复用，保证重试幂等。"""
        existing, _index_found = self._search_archive(
            state.checkpoint_key or "", state
        )
        if existing is not None:
            return existing
        return self.memory.save(
            type="session",
            title=title,
            content=content,
            keywords=keywords,
        )

    def _repair_pending(
        self,
        state: TaskState,
        *,
        journal: dict[str, Any] | None,
        strict_payload: bool,
    ) -> tuple[TaskState, dict[str, Any] | None, bool, str]:
        """修复已提交未归档的 pending 断点。

        顺序：archive → journal → 降级重渲染。修复成功后清 pending，
        并只在 epoch/commit_seq 仍匹配时删除 journal。
        """
        key = state.checkpoint_key
        epoch = state.task_epoch
        commit_seq = state.commit_seq
        if not key or epoch is None or commit_seq is None:
            raise PendingArchiveError(
                "pending state 缺少 checkpoint_key/task_epoch/commit_seq，"
                "无法安全判断归档资格。请检查 state.json，或 clear(force=True)。"
            )

        payload: dict[str, Any] | None = None
        degraded = False
        source = "archive"
        payload, index_found = self._search_archive(key, state)

        journal_matches = bool(
            journal is not None
            and journal.get("checkpoint_key") == key
            and journal.get("task_epoch") == epoch
            and journal.get("commit_seq") == commit_seq
            and isinstance(journal.get("content"), str)
        )

        if payload is None and journal_matches:
            content = str(journal["content"])
            kind = str(journal.get("kind") or "interrupt")
            title = str(
                journal.get("archive_title")
                or self._checkpoint_title(state, kind)
            )
            keywords = str(
                journal.get("archive_keywords")
                or self._checkpoint_keywords(state)
            )
            try:
                record = self.memory.save(
                    type="session",
                    title=title,
                    content=content,
                    keywords=keywords,
                )
            except MemoryBackendError as exc:
                raise PendingArchiveError(
                    "pending 修复需要重新归档，但记忆后端不可用；"
                    "state 保持 pending，稍后重试。"
                ) from exc
            payload = dict(record)
            payload.setdefault("content", content)
            payload.setdefault("type", "session")
            payload.setdefault("title", title)
            payload.setdefault("keywords", keywords)
            source = "journal"

        if payload is None:
            if strict_payload:
                raise ArchivePayloadMissing(
                    f"pending 断点 {key} 既没有可读 archive，也没有匹配的 "
                    "journal；strict_payload=True 拒绝降级。"
                )
            degraded_state = TaskState.from_dict(state.to_dict())
            degraded_state.archive_pending = False
            kind = {
                TaskStatus.COMPLETED: "complete",
                TaskStatus.BLOCKED: "blocked",
            }.get(degraded_state.status, "interrupt")
            content = self._render_checkpoint(
                degraded_state,
                kind=kind,
                reason="pending 修复：archive/journal 原文缺失，按 state 重渲染",
                summary="",
            )
            title = self._checkpoint_title(degraded_state, kind) + " [degraded]"
            keywords = self._checkpoint_keywords(degraded_state) + ",degraded"
            try:
                record = self.memory.save(
                    type="session",
                    title=title,
                    content=content,
                    keywords=keywords,
                )
            except MemoryBackendError as exc:
                raise PendingArchiveError(
                    "pending 修复需要降级归档，但记忆后端不可用；"
                    "state 保持 pending，稍后重试。"
                ) from exc
            payload = dict(record)
            payload.setdefault("content", content)
            payload.setdefault("type", "session")
            payload.setdefault("title", title)
            payload.setdefault("keywords", keywords)
            source = "degraded"
            degraded = True

        record_id = str(payload.get("id") or "") if payload else ""

        with self.store.locked():
            latest = self.store.load()
            if latest is None:
                raise NoActiveTask("pending 修复期间 state 被删除。")
            if (
                latest.task_id != state.task_id
                or latest.task_epoch != epoch
                or latest.commit_seq != commit_seq
            ):
                raise StateConflict(
                    "pending 修复期间状态被其他进程修改；"
                    "不要用旧候选清理 pending。"
                )
            latest.archive_pending = False
            if record_id:
                latest.checkpoint_id = record_id
                if record_id not in latest.checkpoint_memory_ids:
                    latest.checkpoint_memory_ids.append(record_id)
            latest.updated_at = now_iso()
            self.store.save(
                latest,
                expected_revision=latest.revision,
                expected_task_id=latest.task_id,
                expected_task_epoch=latest.task_epoch,
            )
            self._state = latest
        self.store.remove_journal(
            expected_task_epoch=epoch,
            expected_commit_seq=commit_seq,
        )
        return latest, payload, degraded, source

    def _revoke_write_lease(self, state: TaskState, reason: str) -> bool:
        """收回写租约，返回是否确实收回。只改内存，由调用方负责落盘。"""
        if state.access_mode is not AccessMode.WRITABLE and state.write_lease_turn is None:
            return False
        previous_turn = state.write_lease_turn
        state.access_mode = AccessMode.READ_ONLY
        state.write_lease_turn = None
        state.note(
            "write lease revoked"
            + (f" (turn {previous_turn})" if previous_turn is not None else "")
            + (f"：{reason}" if reason else "")
        )
        return True

    def _start_new_turn(self, state: TaskState, reason: str) -> None:
        """递增对话轮次，并确保上一轮写权限不会跨轮继承。"""
        state.turn_index = max(0, state.turn_index) + 1
        revoked = self._revoke_write_lease(state, reason)
        state.note(
            f"turn {state.turn_index} begin"
            + ("；上一轮写权限已收回" if revoked else "")
            + (f"（{reason}）" if reason else "")
        )

    def _new_task_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return f"T_{stamp}_{secrets.token_hex(2)}"

    def _new_task_epoch(self) -> str:
        """任务世代。新任务/legacy 升级时生成，用于区分同名任务。"""
        return secrets.token_hex(16)

    def _search_for_task(self, terms: list[str]) -> list[dict[str, Any]]:
        if not terms:
            try:
                return self.memory.search("", limit=self.max_memories)
            except MemoryBackendError:
                if self.strict_memory:
                    raise
                return []

        found: dict[str, dict[str, Any]] = {}
        for term in terms[: self.max_search_terms]:
            try:
                rows = self.memory.search(term, limit=self.max_memories)
            except MemoryBackendError:
                if self.strict_memory:
                    raise
                continue
            for row in rows:
                rid = str(row.get("id", ""))
                if rid:
                    found.setdefault(rid, row)
            if len(found) >= self.max_memories:
                break
        return list(found.values())[: self.max_memories]

    def _hydrate(
        self, memories: list[dict[str, Any]], count: int
    ) -> list[dict[str, Any]]:
        hydrated: list[dict[str, Any]] = []
        for row in memories[:count]:
            rid = str(row.get("id", ""))
            if not rid:
                continue
            try:
                hydrated.append(self.memory.read_raw(rid))
            except MemoryBackendError:
                if self.strict_memory:
                    raise
        return hydrated

    def _safe_search(self, keyword: str) -> list[dict[str, Any]]:
        try:
            return self.memory.search(keyword, limit=self.max_memories)
        except MemoryBackendError:
            return []

    def _target_status(
        self, kind: str, status: TaskStatus | str | None
    ) -> TaskStatus | None:
        if status is not None:
            return status if isinstance(status, TaskStatus) else TaskStatus(status)
        return {
            "interrupt": TaskStatus.PAUSED,
            "blocked": TaskStatus.BLOCKED,
            "complete": TaskStatus.COMPLETED,
            "phase_complete": None,  # 阶段完成不改变总状态
        }[kind]

    def _start_summary(
        self,
        state: TaskState,
        memories: list[dict[str, Any]],
        context: list[dict[str, Any]],
    ) -> str:
        parts = [
            f"已先检索记忆：命中 {len(memories)} 条，加载 {len(context)} 条原文。",
            f"当前读写门禁：{state.access_mode.value}。",
        ]
        if context:
            parts.append("请先阅读 context 中的原文，再决定是否进入实施。")
        elif memories:
            parts.append("只拿到了摘要；需要细节时按 ID 调用 read_raw_data。")
        else:
            parts.append("没有命中相关历史，本次按新任务处理。")
        if state.access_mode is AccessMode.READ_ONLY:
            parts.append(
                "修改代码前必须调用 enable_writes(原因)；"
                "写权限只对当前对话轮次有效，换轮/断点/恢复后需重新授权。"
            )
        return " ".join(parts)

    def _checkpoint_title(self, state: TaskState, kind: str) -> str:
        short = _short(state.description, 28)
        if kind == "phase_complete":
            title = f"[阶段完成] {state.phase} · {short}"
        elif kind == "blocked":
            title = f"[阻塞] {short} @ {state.phase}"
        elif kind == "complete":
            title = f"[完成] {short}"
        else:
            title = f"[断点] {short} @ {state.phase}"
        if state.checkpoint_key:
            title += f" [{state.checkpoint_key[:8]}]"
        return title

    def _checkpoint_keywords(self, state: TaskState) -> str:
        parts = ["longrun", "checkpoint", "archive", state.task_id]
        if state.task_epoch:
            parts.append(state.task_epoch)
        if state.checkpoint_key:
            parts.append(state.checkpoint_key)
        return ",".join(part for part in parts if part)

    def _render_checkpoint(
        self,
        state: TaskState,
        *,
        kind: str,
        reason: str,
        summary: str,
    ) -> str:
        lines = [
            "[LongRun Checkpoint]",
            f"任务: {_sanitize_marker_text(state.description)}",
            f"任务ID: {_sanitize_marker_text(state.task_id)}",
            f"类型: {kind}",
            f"状态: {state.status.value}",
            f"读写门禁: {state.access_mode.value}",
            f"对话轮次: {state.turn_index}",
            f"阶段: {_sanitize_marker_text(state.phase)}",
        ]
        if reason:
            lines.append(f"触发原因: {_sanitize_marker_text(reason)}")
        if summary:
            lines.append(f"本阶段完成: {_sanitize_marker_text(summary)}")
        if state.completed_steps:
            lines.append("已完成:")
            lines.extend(
                f"  - {_sanitize_marker_text(item)}"
                for item in state.completed_steps[-10:]
            )
        if state.decisions:
            lines.append("关键决策:")
            lines.extend(
                f"  - {_sanitize_marker_text(item)}"
                for item in state.decisions[-10:]
            )
        lines.append(f"下一步: {_sanitize_marker_text(state.next_step or '未指定')}")
        lines.append(f"时间: {now_iso()}")

        # 机器可读的状态标记。recover_from_memory() 靠它重建，不靠自然语言猜测。
        # checkpoint_id 是 archive 后的 best-effort 缓存，不能进入 marker 充当
        # 恢复资格；权威指针是 checkpoint_key + commit_seq。
        marker = state.to_dict()
        marker.pop("history", None)
        marker.pop("checkpoint_id", None)
        marker.pop("checkpoint_memory_ids", None)
        marker_json = json.dumps(marker, ensure_ascii=False, separators=(",", ":"))
        # marker 里的用户字段（description/next_step 等）可能包含
        # `<!--` / `-->`。在 JSON 字符串内转成 < / >：
        # 解析后的值不变，但整行不会再出现可被当成 marker 边界的字面量。
        marker_json = marker_json.replace("<!--", "\\u003c!--").replace(
            "-->", "--\\u003e"
        )
        lines.append("")
        lines.append("<!-- longrun-state: " + marker_json + " -->")
        return "\n".join(lines)
