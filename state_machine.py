"""长任务状态机 —— 从 engineering-workflow 的模块状态语义移植而来。

原项目用 `project-profile.json` 管理模块状态：

    pending → in_progress → waiting_verification → completed
                  │                │
                  └── blocked ←────┘

本模块保留这四个状态，并新增：

    paused    token 预警 / 主动断点 / 进程被杀 → 可由 resume() 回到 in_progress
    failed    不可恢复的终态

**所有状态变更必须经过 `TaskState.transition()`**，非法流转直接抛异常。
状态机存在的意义，就是让「跳跃式前进」在代码层面不可能发生 ——
只写在 Rule 里的约束，模型随时可以忘掉；写进状态机的约束，忘不掉。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class TaskStatus(str, Enum):
    """任务状态。值刻意用字符串，方便直接写进 JSON 与记忆库。"""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    BLOCKED = "blocked"
    WAITING_VERIFICATION = "waiting_verification"
    COMPLETED = "completed"
    FAILED = "failed"


class AccessMode(str, Enum):
    """读写门禁。

    READ_ONLY    对话态 / 调研 / 规划 / 读历史。可以查记忆，但不能改代码。
    WRITABLE     执行态。只有显式调用 enable_writes() 才会进入，
                 且只在当前 turn_index 内有效；下一轮对话、checkpoint
                 或 resume 都会把它收回 READ_ONLY。
    """

    READ_ONLY = "read_only"
    WRITABLE = "writable"


# 状态流转白名单。任何不在这里的跳转都会被拒绝。
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.IN_PROGRESS},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.PAUSED,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_VERIFICATION,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.PAUSED: {TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.BLOCKED: {TaskStatus.IN_PROGRESS, TaskStatus.FAILED},
    TaskStatus.WAITING_VERIFICATION: {
        TaskStatus.IN_PROGRESS,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: set(),
}

TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED})

# 历史只保留最近 N 条，避免 state.json 无限增长。
MAX_HISTORY = 20

# v4 状态 schema。缺失 schema_version 的旧状态按 0 处理；
# 高于这个版本的 state 明确拒绝，避免新字段被旧代码静默丢掉。
STATE_SCHEMA_VERSION = 4


class StateMachineError(RuntimeError):
    """状态机相关错误的基类。"""


class TransitionError(StateMachineError):
    """尝试了白名单之外的流转。"""


class StateCorruptError(StateMachineError):
    """state.json 无法解析。宁可报错，也不要静默丢失进度。"""


class StateConflict(StateMachineError):
    """状态文件被其他进程修改，当前写入基于过期 revision。"""


class StatePendingError(StateMachineError):
    """状态已提交，但断点归档尚未完成；此时不能清除或覆盖。"""


class StateLockTimeout(StateMachineError):
    """等待状态文件跨进程锁超时。"""


# 跨进程锁的默认等待时间。写状态是毫秒级操作，10 秒足够；
# 真等到超时说明有进程持锁卡死，应该报错而不是无限阻塞。
DEFAULT_LOCK_TIMEOUT = 10.0
LOCK_POLL_INTERVAL = 0.05

# 同进程多线程先串行化，再拿跨进程文件锁。
# _LOCK_STATE 记录当前线程已经持有的锁（path → (depth, fd)），
# 让 StateStore.locked() 在同一线程内可重入，避免 save() 嵌套时重复加锁。
_STATE_THREAD_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def _held_locks() -> dict[str, tuple[int, int]]:
    held = getattr(_LOCK_STATE, "held", None)
    if held is None:
        held = {}
        _LOCK_STATE.held = held
    return held


def _acquire_file_lock(path: Path, timeout: float) -> int:
    """获取跨进程排他锁，返回持有的文件描述符。

    Windows 用 msvcrt.locking（字节范围锁），POSIX 用 fcntl.flock。
    非阻塞轮询 + 超时，不用阻塞式加锁：卡住的进程不能拖死整个仓库。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        while True:
            try:
                if os.name == "nt":
                    # msvcrt 按当前文件位置锁 1 字节；空文件也能锁，
                    # 但先写一个字节，兼容不同 Windows 版本的行为差异。
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except OSError:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(
                        f"等待状态文件锁超时（{timeout:.2f}s）：{path}。"
                        "另一个进程可能正在写状态；稍后重试，不要删除锁文件。"
                    ) from None
                time.sleep(LOCK_POLL_INTERVAL)
    except BaseException:
        os.close(fd)
        raise


def _release_file_lock(fd: int) -> None:
    try:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextmanager
def _state_file_lock(path: Path, timeout: float) -> Iterator[None]:
    with _STATE_THREAD_LOCK:
        key = str(path.resolve())
        held = _held_locks()
        if key in held:
            depth, fd = held[key]
            held[key] = (depth + 1, fd)
            try:
                yield
            finally:
                depth, fd = held[key]
                if depth <= 1:
                    _release_file_lock(fd)
                    del held[key]
                else:
                    held[key] = (depth - 1, fd)
            return

        fd = _acquire_file_lock(path, timeout)
        held[key] = (1, fd)
        try:
            yield
        finally:
            depth, fd = held[key]
            if depth <= 1:
                _release_file_lock(fd)
                del held[key]
            else:
                held[key] = (depth - 1, fd)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TaskState:
    """一个长任务的完整状态。

    字段分四类：
    1. 身份：task_id / description / created_at
    2. 执行：status / access_mode / phase / completed_steps / next_step
    3. 对话边界：turn_index / write_lease_turn
       —— 写权限是「单轮租约」，不是持久开关；换轮即失效。
    4. 记忆：decisions / checkpoint_id / checkpoint_memory_ids

    `checkpoint_memory_ids` 保存所有断点记忆的 ID。恢复时先看最新的那条，
    但保留全部 ID 是为了让 Agent 能按需回溯更早的断点。
    """

    task_id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    access_mode: AccessMode = AccessMode.READ_ONLY
    # 对话轮次。每次 begin_turn / resume / recover_from_memory 递增。
    turn_index: int = 0
    # 写租约绑定的轮次。只有 write_lease_turn == turn_index 时 writable 才有效。
    write_lease_turn: int | None = None
    phase: str = "研究"
    completed_steps: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    next_step: str = ""
    checkpoint_id: str | None = None
    checkpoint_memory_ids: list[str] = field(default_factory=list)
    # v4 提交协议字段。checkpoint_key 是权威指针，checkpoint_id 只是
    # archive 成功后的 memory record id，不能作为恢复资格。
    schema_version: int = STATE_SCHEMA_VERSION
    task_epoch: str | None = None
    checkpoint_key: str | None = None
    commit_seq: int | None = None
    archive_pending: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    history: list[dict[str, Any]] = field(default_factory=list)
    # 状态文件版本号：每次成功 save 由 StateStore 递增。
    # 读-改-写路径用它做 CAS，防止多进程互相覆盖。
    revision: int = 0
    # 未知字段 round-trip：旧/新版本互相打开时，不要静默丢字段。
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._normalize_write_lease()
        # pending 是硬停写状态：即使构造参数带 writable，也不允许保留租约。
        if self.archive_pending:
            self.access_mode = AccessMode.READ_ONLY
            self.write_lease_turn = None

    def _normalize_write_lease(self) -> None:
        """维护写租约不变量：只有 in_progress 且绑定轮次时才允许 writable。

        旧版 JSON、手工构造的 `TaskState`、以及直接改字段的调用方都可能留下
        `paused + writable` 之类的组合。在状态机内部归一化，比要求每个调用方
        记得收回更可靠。
        """
        if self.access_mode is not AccessMode.WRITABLE:
            # READ_ONLY 不应携带租约；旧数据可能残留 write_lease_turn。
            self.write_lease_turn = None
            return
        if self.status is not TaskStatus.IN_PROGRESS or self.write_lease_turn is None:
            self.access_mode = AccessMode.READ_ONLY
            self.write_lease_turn = None

    # -- 流转 ---------------------------------------------------------------

    def can_transition_to(self, target: TaskStatus) -> bool:
        return target in TRANSITIONS.get(self.status, set())

    def transition(self, target: TaskStatus, reason: str = "") -> None:
        if not isinstance(target, TaskStatus):
            target = TaskStatus(target)
        if target not in TRANSITIONS.get(self.status, set()):
            allowed = ", ".join(sorted(s.value for s in TRANSITIONS.get(self.status, set())))
            raise TransitionError(
                f"非法状态流转：{self.status.value} → {target.value}。"
                f"当前状态允许的目标：{allowed or '无（终态）'}。"
            )
        had_lease = (
            self.access_mode is AccessMode.WRITABLE
            or self.write_lease_turn is not None
        )
        self.history.append(
            {
                "from": self.status.value,
                "to": target.value,
                "reason": reason,
                "at": now_iso(),
            }
        )
        self.history = self.history[-MAX_HISTORY:]
        self.status = target
        self.updated_at = now_iso()

        # 任何状态流转都结束当前写租约：跨状态不继承写权限。
        # 这比"只在离开 in_progress 时收回"更严格，也覆盖了
        # pending/paused/blocked 进入 in_progress 时不应自动获得写权限。
        self.access_mode = AccessMode.READ_ONLY
        self.write_lease_turn = None
        if had_lease:
            self.note("write lease revoked：状态流转后必须重新 enable_writes()")

    def note(self, message: str) -> None:
        """记录一条不改状态的事件（如切换读写门禁）。"""
        self.history.append(
            {
                "from": self.status.value,
                "to": self.status.value,
                "reason": message,
                "at": now_iso(),
            }
        )
        self.history = self.history[-MAX_HISTORY:]
        self.updated_at = now_iso()

    # -- 序列化 -------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """显式转换枚举。直接 asdict() 会留下 Enum 对象，json.dumps 会炸。"""
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "description": self.description,
            "status": self.status.value,
            "access_mode": self.access_mode.value,
            "turn_index": self.turn_index,
            "write_lease_turn": self.write_lease_turn,
            "phase": self.phase,
            "completed_steps": list(self.completed_steps),
            "decisions": list(self.decisions),
            "next_step": self.next_step,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_memory_ids": list(self.checkpoint_memory_ids),
            "schema_version": self.schema_version,
            "task_epoch": self.task_epoch,
            "checkpoint_key": self.checkpoint_key,
            "commit_seq": self.commit_seq,
            "archive_pending": self.archive_pending,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "revision": self.revision,
            "history": list(self.history),
        }
        # 已知字段优先；未知字段只在后面没有同名键时写回。
        for key, value in self.extra.items():
            payload.setdefault(key, value)
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskState:
        schema_version = int(data.get("schema_version", 0) or 0)
        if schema_version > STATE_SCHEMA_VERSION:
            raise StateCorruptError(
                f"状态 schema_version={schema_version} 高于当前支持的 "
                f"{STATE_SCHEMA_VERSION}。请升级框架后再打开这个项目，"
                "不要让旧二进制静默丢掉新字段。"
            )

        known = {
            "task_id", "description", "status", "access_mode", "turn_index",
            "write_lease_turn", "phase", "completed_steps", "decisions",
            "next_step", "checkpoint_id", "checkpoint_memory_ids",
            "schema_version", "task_epoch", "checkpoint_key", "commit_seq",
            "archive_pending", "created_at", "updated_at", "history", "revision",
        }
        extra = {key: value for key, value in data.items() if key not in known}
        commit_seq = data.get("commit_seq")

        # `__post_init__` 会归一化非执行状态的写租约，旧 JSON 不能绕过门禁。
        return cls(
            task_id=str(data["task_id"]),
            description=str(data.get("description", "")),
            status=TaskStatus(data.get("status", "pending")),
            access_mode=AccessMode(data.get("access_mode", "read_only")),
            turn_index=int(data.get("turn_index", 0) or 0),
            write_lease_turn=(
                None
                if data.get("write_lease_turn") is None
                else int(data["write_lease_turn"])
            ),
            phase=str(data.get("phase", "研究")),
            completed_steps=list(data.get("completed_steps") or []),
            decisions=list(data.get("decisions") or []),
            next_step=str(data.get("next_step", "")),
            checkpoint_id=data.get("checkpoint_id"),
            checkpoint_memory_ids=list(data.get("checkpoint_memory_ids") or []),
            schema_version=schema_version,
            task_epoch=(
                str(data["task_epoch"]) if data.get("task_epoch") else None
            ),
            checkpoint_key=(
                str(data["checkpoint_key"]) if data.get("checkpoint_key") else None
            ),
            commit_seq=int(commit_seq) if commit_seq is not None else None,
            archive_pending=bool(data.get("archive_pending", False)),
            created_at=str(data.get("created_at") or now_iso()),
            updated_at=str(data.get("updated_at") or now_iso()),
            history=list(data.get("history") or [])[-MAX_HISTORY:],
            revision=int(data.get("revision", 0) or 0),
            extra=extra,
        )


class StateStore:
    """把任务状态落到本地 JSON。

    为什么还要本地落盘，既然记忆库里也有？

    - 记忆库是**冷数据**：适合历史回溯，不适合每次状态判断都去 MCP 绕一圈
    - state.json 是**热状态**：resume 时毫秒级读取，且不依赖 MCP 进程活着

    两者互为备份：本地丢了从记忆里重建，记忆写失败则先落本地待补。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
    ) -> None:
        self.root = Path(root).resolve()
        self.dir = self.root / ".longrun"
        self.path = self.dir / "state.json"
        # v4 单槽 journal：CAS 成功后、archive 完成前的本地凭证。
        self.journal_path = self.dir / "checkpoint_journal.json"
        # v3 兼容：只读检测，v4 不再写这个文件。
        self.legacy_fallback_path = self.dir / "pending_checkpoint.json"
        self.fallback_path = self.legacy_fallback_path
        # 锁文件独立于 state.json，且永不删除：删除会让两个进程
        # 各持一个不同 inode 的锁，等于没锁。
        self.lock_path = self.dir / "state.lock"
        self.lock_timeout = max(0.0, float(lock_timeout))

    @contextmanager
    def locked(self) -> Iterator[None]:
        """持有跨进程状态锁。

        所有「读 state → 改 state → save」的路径都必须包在这里面。
        load() 也会自行获取这把锁，避免 Windows 上 os.replace 与
        正在读取 state.json 的句柄冲突。
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        with _state_file_lock(self.lock_path, self.lock_timeout):
            yield

    def load(self) -> TaskState | None:
        """读取状态。返回的 revision 是这份快照的版本号。

        文件存在时持锁读取：Windows 的 os.replace 需要目标文件没有
        其它打开句柄；读者和写者都用同一把锁，才不会偶发替换失败。
        """
        if not self.path.exists():
            return None
        with self.locked():
            return self._load_unlocked()

    def _load_unlocked(self) -> TaskState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptError(
                f"状态文件损坏：{self.path}。"
                "不要猜，先修 JSON 或从记忆库恢复（recover_from_memory）。"
            ) from exc
        return TaskState.from_dict(data)

    def save(
        self,
        state: TaskState,
        *,
        expected_revision: int | None = None,
        expected_task_id: str | None = None,
        expected_task_epoch: str | None = None,
        minimum_revision: int | None = None,
    ) -> None:
        """原子保存状态，并递增 revision。

        Args:
            expected_revision: 调用方读到的 revision。提供时做 CAS：
                磁盘上的 revision 与它不一致就抛 StateConflict，
                绝不覆盖其他进程刚写入的进度。传 None 表示不做 CAS
                （仅用于初始化或明确接受覆盖的场景）。
            expected_task_id: 可选的 task_id CAS。防止 revision 相同但任务
                已被替换的 ABA 场景。
            expected_task_epoch: 可选的 task_epoch CAS。新任务换 epoch 后，
                旧进程即使 revision 相同也不能写入。
            minimum_revision: 新 revision 的下限。仅用于从 journal 重建
                state：不能让 revision 倒退到旧断点之前，否则下一次
                checkpoint 的 commit_seq 会小于历史断点，破坏恢复排序。

        跨进程锁 + 唯一临时文件 + os.replace：
        - 锁保证「读 revision → 比较 → 写」是一个原子临界区；
        - 唯一 tmp 避免两个进程共用 state.json.tmp 互删；
        - os.replace 保证读者只会看到旧文件或新文件。
        """
        with self.locked():
            current = self._load_unlocked()
            current_revision = current.revision if current is not None else 0

            if current is None:
                if expected_task_id is not None or expected_task_epoch is not None:
                    raise StateConflict(
                        "状态文件不存在，但调用方期望任务 "
                        f"task_id={expected_task_id!r} / "
                        f"task_epoch={expected_task_epoch!r}；请重新加载。"
                    )
            else:
                if (
                    expected_task_id is not None
                    and current.task_id != expected_task_id
                ):
                    raise StateConflict(
                        f"任务身份冲突：期望 task_id={expected_task_id!r}，"
                        f"当前 task_id={current.task_id!r}。"
                    )
                if (
                    expected_task_epoch is not None
                    and current.task_epoch != expected_task_epoch
                ):
                    raise StateConflict(
                        f"任务世代冲突：期望 task_epoch="
                        f"{expected_task_epoch!r}，当前 task_epoch="
                        f"{current.task_epoch!r}。"
                    )

            if (
                expected_revision is not None
                and current_revision != expected_revision
            ):
                raise StateConflict(
                    f"状态版本冲突：期望 revision={expected_revision}，"
                    f"当前 revision={current_revision}（{self.path}）。"
                    "另一个进程已更新状态；请重新 resume() 后再操作。"
                )

            new_revision = current_revision + 1
            if minimum_revision is not None:
                new_revision = max(new_revision, int(minimum_revision))
            previous_revision = state.revision
            previous_schema = state.schema_version
            state.revision = new_revision
            if state.schema_version < STATE_SCHEMA_VERSION:
                # 旧状态第一次被 v4 保存时升级 schema；未知字段仍在 extra 里。
                state.schema_version = STATE_SCHEMA_VERSION
            try:
                self._atomic_write(state)
            except BaseException:
                # 写失败时不要在内存里留下一个磁盘上不存在的 revision。
                state.revision = previous_revision
                state.schema_version = previous_schema
                raise

    def _atomic_write(self, state: TaskState) -> None:
        self._atomic_write_json(self.path, state.to_dict())

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        """唯一 tmp + os.replace 的 JSON 原子写。"""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(
            f"{path.stem}.{os.getpid()}.{uuid.uuid4().hex}.json.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    # -- v4 journal ---------------------------------------------------------

    def load_journal(self) -> dict[str, Any] | None:
        """读取单槽 journal。损坏时明确报错，不能静默当成没有。"""
        with self.locked():
            return self._load_journal_unlocked()

    def _load_journal_unlocked(self) -> dict[str, Any] | None:
        if not self.journal_path.exists():
            return None
        try:
            data = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptError(
                f"断点 journal 损坏：{self.journal_path}。"
                "不要猜，先检查文件或从 archive/state 恢复。"
            ) from exc
        if not isinstance(data, dict):
            raise StateCorruptError(
                f"断点 journal 结构错误：{self.journal_path} 顶层必须是对象。"
            )
        return data

    def write_journal(self, payload: dict[str, Any]) -> Path:
        """写单槽 journal，返回路径。

        已有 journal 且不属于同一个断点、或 commit_seq 更高时拒绝覆盖；
        调用方必须先 resume()/retry_pending_checkpoint() 对账。
        """
        with self.locked():
            existing = self._load_journal_unlocked()
            if existing is not None:
                old_key = existing.get("checkpoint_key")
                new_key = payload.get("checkpoint_key")
                old_seq = existing.get("commit_seq")
                new_seq = payload.get("commit_seq")
                if old_key != new_key:
                    if (
                        old_seq is not None
                        and new_seq is not None
                        and int(old_seq) >= int(new_seq)
                    ):
                        raise StateConflict(
                            "已存在更新的 checkpoint journal "
                            f"(key={old_key!r}, commit_seq={old_seq!r})；"
                            "请先 resume() 或 retry_pending_checkpoint() 对账。"
                        )
            self._atomic_write_json(self.journal_path, payload)
            return self.journal_path

    def remove_journal(
        self,
        *,
        expected_task_epoch: str | None = None,
        expected_commit_seq: int | None = None,
    ) -> bool:
        """删除 journal，但只在 epoch/commit_seq 匹配时删除。

        Returns:
            True 表示删除成功；False 表示 journal 不存在或与期望不匹配。
        """
        with self.locked():
            existing = self._load_journal_unlocked()
            if existing is None:
                return False
            if (
                expected_task_epoch is not None
                and existing.get("task_epoch") != expected_task_epoch
            ):
                return False
            if (
                expected_commit_seq is not None
                and existing.get("commit_seq") != expected_commit_seq
            ):
                return False
            self.journal_path.unlink(missing_ok=True)
            return True

    def clear(self, *, force: bool = False) -> None:
        """清除本地状态。

        pending 时默认拒绝；`force=True` 会同时删除 state、journal 和
        v3 legacy fallback。只有用户明确接受丢断点时才用 force。
        """
        with self.locked():
            state = self._load_unlocked()
            if state is not None and state.archive_pending and not force:
                raise StatePendingError(
                    f"任务 {state.task_id} 还有待归档断点 "
                    f"(checkpoint_key={state.checkpoint_key!r})，拒绝 clear()。"
                    "先 resume()/retry_pending_checkpoint()；"
                    "确需丢弃请用 clear(force=True)。"
                )
            self.path.unlink(missing_ok=True)
            self.journal_path.unlink(missing_ok=True)
            if force:
                self.legacy_fallback_path.unlink(missing_ok=True)
