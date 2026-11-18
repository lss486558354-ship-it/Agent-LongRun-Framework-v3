# PROJECT_INDEX

> 最后更新：2026-11-18（v0.4.1 发布收尾：lint 与测试口径）
> 维护规则：每次代码修改后追加「修改历史」，并刷新此处。

## 1. 项目基本信息

| 项 | 内容 |
|----|------|
| 名称 | Agent-LongRun-Framework |
| 版本 | 0.4.1 |
| 定位 | 把 Agent 执行状态机与项目级 MCP 记忆路由耦合的长任务工作流；单轮写租约 + state CAS 断点提交协议 |
| 语言 | Python 3.10+（开发环境 3.12.0） |
| 依赖 | `mcp>=2.0,<3.0`；开发依赖 pytest、ruff |
| 入口 | `workflow_manager.LongRunAgent`；CLI `agent-longrun demo` |
| 上游 | `engineering-workflow`（状态语义/断点格式）、`agent-memory-router-v2`（项目级 MCP 记忆） |
| 远端 | https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3 |
| License | MIT |
| 项目数据 | `<项目根>/.longrun/state.json`、`.longrun/checkpoint_journal.json`、`<项目根>/memory.db`、`<项目根>/raw_data/` |

## 2. 目录结构

```text
Agent-LongRun-Framework-v3/
├── state_machine.py        # TaskStatus(7)、AccessMode、TRANSITIONS、TaskState、StateStore
├── memory_client.py        # MemoryBackend 协议 + MCP/Local/InMemory 三实现 + 项目根解析
├── workflow_manager.py     # LongRunAgent 门面、单轮写租约、断点/恢复逻辑
├── cli.py                  # console script: agent-longrun demo
├── tests/
│   ├── conftest.py         # 把仓库根加入 sys.path
│   ├── test_state_machine.py
│   ├── test_workflow_manager.py
│   ├── test_v4_protocol.py
│   ├── test_v4_recovery.py
│   ├── test_target2_orphan_checkpoint.py
│   └── test_cli.py
├── examples/
│   ├── 01_interrupted_task.py
│   ├── 02_phase_archive.py
│   └── README.md
├── rules/
│   ├── 00-entry.md
│   ├── 01-memory-first.md
│   ├── 02-checkpoint-archive.md
│   └── 03-execution-boundary.md
├── skills/auto-dev-workflow/SKILL.md
├── .github/workflows/tests.yml
├── .cursorrules / CLAUDE.md
├── README.md               # English
├── README.zh-CN.md         # 简体中文
├── CHANGELOG.md
├── PROJECT_INDEX.md
├── pyproject.toml
├── requirements.txt / requirements-dev.txt
├── .gitignore / .gitattributes
└── LICENSE
```

## 3. 核心模块

### state_machine.py

- `TaskStatus`：`pending / in_progress / paused / blocked / waiting_verification / completed / failed`
- `AccessMode`：`read_only` / `writable`
- `TRANSITIONS`：白名单流转；`completed`、`failed` 无出口
- `TaskState`：任务上下文（描述、阶段、已完成步骤、决策、断点 id、history、
  `turn_index`、`write_lease_turn`、`revision`）；v4 新增
  `schema_version`、`task_epoch`、`checkpoint_key`、`commit_seq`、
  `archive_pending`、`extra`（未知字段 round-trip）
- `StateStore(root, lock_timeout=10.0)`：`<root>/.longrun/state.json` 热状态；
  `.longrun/state.lock` 跨进程锁；唯一 `state.<pid>.<uuid>.json.tmp` +
  `os.replace`；`revision/CAS` + `task_id/task_epoch` CAS；损坏抛
  `StateCorruptError`，冲突抛 `StateConflict`，锁超时抛 `StateLockTimeout`
- v4 journal：`.longrun/checkpoint_journal.json` 单槽；`load_journal()` /
  `write_journal()` / `remove_journal(expected_task_epoch, expected_commit_seq)`；
  已有更高 `commit_seq` 的其他 key 拒绝覆盖
- `clear(force=False)`：`archive_pending` 时抛 `StatePendingError`；
  `force=True` 删除 state、journal 与 v3 legacy fallback
- `from_dict()` 拒绝未来 `schema_version`；旧 schema 无 epoch/key/seq 时按
  legacy 加载，保存时升级到 v4
- `load()` 在文件存在时也持锁，避免 Windows `os.replace` 与打开中的句柄冲突
- `transition()` 校验流转、记录 history（最多 20 条），并强制收回写租约；
  `__post_init__()` / `from_dict()` 归一化非 `in_progress` 状态的 writable 租约；
  `archive_pending=True` 强制只读

### memory_client.py

- `MemoryBackend` Protocol：`search / read_raw / save / link`
- `_resolve_project_path()`：`project_path` > `home`（v0.1 别名）> `MEMORY_ROUTER_HOME` > cwd
- `_default_router_path()`：`AGENT_MEMORY_ROUTER_PATH` > 已安装的
  `mcp_server` / `memory_core` 模块（`importlib.util.find_spec`）>
  本文件同级/上一级 > cwd > v0.1 回退路径
- `MCPMemoryBackend(project_path=...)`：stdio 拉起 `mcp_server.py`，参数带
  `--project_path <项目根>`，cwd 设为项目根；强制 UTF-8；`ok=false` 抛 `MemoryBackendError`
- `LocalMemoryBackend(project_path=...)`：import `memory_core` 后调用
  `configure(project_path)` + `init_db()`；同进程切换项目会报错
- `InMemoryBackend`：测试用，记录 `calls` 顺序，ID 前缀 D/C/I/N/S/R/M

### workflow_manager.py

- `LongRunAgent(root=...)`：
  - `project_root = store.root`（传入 store 时）或 `root/LONGRUN_HOME/cwd`
  - `StateStore(project_root)`
  - 默认 `MCPMemoryBackend(project_path=project_root)`
  - 保证热状态与冷记忆属于同一个项目
- `start_task()`：锁内检查磁盘未完成任务 → 检索 → hydrate →
  `PENDING→IN_PROGRESS` → 锁内复查并落盘；固定 `read_only`，不接受
  `access_mode`（旧调用硬报错）
- `checkpoint()`（v4 B 协议）：锁内快照 → 收回写租约 → 副本上改状态 →
  生成 `checkpoint_key/commit_seq`、`archive_pending=True` → 渲染 →
  **state CAS（唯一提交点）** → 写 journal → `save_memory` → 锁内清 pending、
  记 `checkpoint_id` → 删 journal → 返回；CAS 失败抛 `StateConflict`，
  CAS 后归档失败抛 `CheckpointArchiveError`，都不返回断点
- `resume()`：state 缺失时用 journal 重建（`minimum_revision=commit_seq+1`，
  保留 journal 原文）；journal 领先默认抛 `StateBehindJournal`，
  `adopt_journal=True` 才采纳；pending 按 archive → journal → 降级重渲染修复
- `retry_pending_checkpoint()`：只补归档、清 pending，不改状态/轮次
- `retry_legacy_checkpoint()`：显式处理 v3 `pending_checkpoint.json`
- `recover_from_memory()`：任务/epoch/key 过滤；同任务取最高 `commit_seq`；
  不同 epoch 抛 `AmbiguousRecovery`；活动任务默认 `ActiveTaskExists`；
  终态默认跳过；legacy 需 `allow_legacy=True`；raw 缺失抛
  `ArchivePayloadMissing`
- `complete_phase()`：状态保持 `in_progress`，阶段写入 `completed_steps`
- `finish()`：归档后进 `completed`
- `note_blocked()`：`blocked`
- 门禁：`enable_writes / disable_writes / require_writable`；
  `begin_turn / end_turn` 管理轮次边界；所有状态变更在锁内重新读盘；
  `status()` 读磁盘；pending 时硬停写，lifecycle API 放行
- 单轮写租约：`enable_writes()` 仅在 `in_progress` 且原因非空时设置
  `write_lease_turn=turn_index`；`require_writable()` 从磁盘读取最新状态，
  同时校验状态、模式与轮次；过期租约先落盘收回，再抛 `WriteGateViolation`
- `save_decision()`：pending 时拒绝；`checkpoint_id` 缺失时按
  `checkpoint_key` 找 archive，再建 `related_to`
- marker：唯一合法 marker；用户文本与 marker JSON 中的 `<!--` / `-->` 转义
- `extract_keywords()`：拉丁词 + 中文二字滑窗，过滤停用词与全虚词片段

## 4. 数据流

```text
LongRunAgent(root=项目根)
  → MCPMemoryBackend(project_path=项目根) + StateStore(项目根)
  → start_task
      → Memory.search（只查 <项目根>/memory.db）
      → Memory.read_raw（top 1）
      → StateStore.save（锁内 CAS，revision+1，<项目根>/.longrun/state.json）
  → 执行（read_only → enable_writes → writable，仅当前 turn_index）
  → checkpoint
      → 收回写租约（writable → read_only）
      → StateStore.save（CAS 提交 status / archive_pending / checkpoint_key /
        commit_seq / revision）                      ← 唯一提交点
      → journal（checkpoint_journal.json，单槽）
      → Memory.save（session，title/keywords 含完整 checkpoint_key）
      → 清 archive_pending、记 checkpoint_id、删 journal
      → 返回断点
  → 新用户消息 begin_turn（turn_index+1，收回租约）
  → resume：state → journal → memory 修复 pending；强制 read_only
  → recover_from_memory（本地无可保护任务时；必须使用同一个 root）
```

MCP 子进程启动参数：

```text
<python> <router>/mcp_server.py --project_path <项目根>
cwd = <项目根>
env = PYTHONIOENCODING=utf-8, PYTHONUTF8=1
```

## 5. 用户需求推断

- 用户要把「记忆机制」固化进 Rule + Skill，而不仅是做成工具
- v0.2 要求记忆按项目自治：每个项目独立 `.mcp.json`、数据库、原文目录
- 要求状态与记忆必须配套：同一个 `root` 同时决定 `.longrun/` 与默认后端
- 要求「换机器恢复」仍然可用：热状态可丢，冷记忆必须能重建
- 重视可验证性：README 有 Mermaid 架构图、示例可运行、测试通过
- 重视简历可展示性：命名、结构、文档都要能直接公开
- 偏好中文文档与注释，代码标识符英文

## 6. 编码习惯

- 中文 docstring 与行内注释，先说明「为什么」
- 小模块优先，单文件承担单一职责
- 数据类 + Enum 而非裸 dict
- 显式异常类型，失败路径写测试
- 原子写（tmp → replace）
- 测试使用 InMemoryBackend 断言调用顺序，不 mock 内部实现
- 兼容旧参数时保留别名（`home`），但新代码优先用新名字（`project_path`）

## 7. 修改历史

### 2026-10-24 · 初始化

- 新建状态机、记忆后端协议、`LongRunAgent` 胶水层
- 38 个测试通过（状态机 10 + 工作流 28）
- 示例 1（中断恢复）在 Local 与真实 MCP stdio 下均跑通
- 示例 2（阶段归档 + 换机器恢复）在 Local 下跑通
- 补齐 rules、skill、`.cursorrules`、`CLAUDE.md`、README、工程元文件
- 初始化 git 并推送远端

### 2026-10-25 · v0.2 项目自治

- `LongRunAgent(root=...)` 先解析项目根，再创建 `StateStore` 与默认后端
- 默认 `MCPMemoryBackend(project_path=...)`，子进程参数带 `--project_path`
- `LocalMemoryBackend` 改用 `memory_core.configure(project_path)`，删除对
  `MEMORY_ROUTER_HOME` 的强依赖（保留为兼容回退）
- `home=` 保留为 `project_path=` 的旧别名
- `_default_router_path()` 优先同级 `agent-memory-router-v2`，回退 v0.1
- 示例改为 `project_path=临时项目根`，验证 `memory.db` / `raw_data/` 落在项目内
- 新增 4 个项目绑定测试，测试总数 42
- 更新 README / CLAUDE.md / `.cursorrules` / rules / skill / examples / 本索引

### 2026-11-04 · v0.3 单轮写租约

- `TaskState` 新增 `turn_index` / `write_lease_turn`，旧状态自动兼容
- `enable_writes()` 改为只授权当前轮；`begin_turn()` / `end_turn()` /
  `checkpoint()` / `resume()` / `recover_from_memory()` 均收回租约
- `require_writable()` 同时校验模式与租约轮次；过期租约抛 `WriteGateViolation`
- 旧 v0.2 `writable` 状态在新实例加载时强制降为 `read_only`
- `checkpoint()` 在渲染机器标记前收回租约，标记恒为 `read_only`
- 修复 pip 安装后 router 定位：`importlib.util.find_spec` 查找已安装模块
- 新增 `agent-longrun demo`（`cli.py`）与 `tests/test_cli.py`
- 新增英文 `README.md`，中文版移至 `README.zh-CN.md`
- 新增 `CHANGELOG.md` 与 GitHub Actions（Python 3.10–3.12）
- 测试总数 52；示例与 demo 全部跑通

### 2026-11-14 · v0.3 写门禁不变量（PR-1）

- `start_task()` 删除公开 `access_mode` 参数；传入即 `LongRunError`，不存在启动即写
- `enable_writes()` 仅允许 `in_progress` 且 `reason` 非空
- `require_writable()` 增加 `status is in_progress` 校验
- `TaskState.transition()` 在任何状态流转后强制收回写租约；
  `__post_init__()` / `from_dict()` 归一化非执行状态的 writable 租约
- 新增 14 个测试用例，其中 12 个在旧代码上失败；测试总数 66 passed + 1 skipped
- 更新 `rules/00-entry.md`、`rules/03-execution-boundary.md`、
  `skills/auto-dev-workflow/SKILL.md`
- 备份：`.backups/20261114_v3-fix/`；问题记录：`BUG_DESCRIPTION.md`

### 2026-11-14 · 状态文件并发安全（PR-2）

- `StateStore` 新增 `.longrun/state.lock` 跨进程锁（Windows `msvcrt` /
  POSIX `fcntl`），默认 10s 超时、50ms 轮询；锁文件永不删除
- 保存改为唯一 `state.<pid>.<uuid>.json.tmp` + `os.replace`
- `TaskState` 新增 `revision`；`StateStore.save(expected_revision=...)` 做 CAS，
  冲突抛 `StateConflict`，失败回滚内存 revision
- `load()` 在文件存在时也持锁，避免 Windows `os.replace` 与打开句柄冲突
- 所有读-改-写路径锁内重新读盘；`start_task` 检索前后双重检查；
  `checkpoint` 锁外写记忆 + 锁内 CAS 提交
- 新增 7 个并发/冲突测试；73 passed + 1 skipped；多进程三件套连续 10 轮通过；
  ruff 全绿
- 备份：`.backups/20261114_v3-fix-pr2/`；`rules/03-execution-boundary.md`
  新增「状态文件并发」小节

### 2026-11-14 · 白帽悬赏文案（只改文档）

- `README.md` / `README.zh-CN.md` 新增「Security & white-hat challenge /
  安全与白帽挑战」：靶子 1 断点标记注入（悬赏）、靶子 2 断点孤儿（挑战）；
  `.longrun/state.lock` 替换列为应用层威胁模型之外，不作为悬赏
- `RELEASE_NOTES_v0.3.0.md` 同步悬赏摘要
- README 测试计数从 52 更新为 73 passed + 1 skipped；修正「状态文件没有加锁」
  的过期描述（PR-2 已加 `state.lock`、唯一 tmp、`revision/CAS`）
- 本轮只改文档，不改源码；任务保持 `in_progress` / `read_only`
- 备份：`.backups/20261114_bounty-docs/`

### 2026-11-15 · v2/v3 交叉链接与 v2 Topics

- `README.md` / `README.zh-CN.md` 顶部新增指向 `agent-memory-router-v2` 的配套记忆路由器链接
- `agent-memory-router-v2` README 顶部新增指向本框架的交叉链接
- 为 `agent-memory-router-v2` 补 GitHub Topics：mcp、mcp-server、ai-agents、llm、memory、
  sqlite、local-first、python、long-context、agent-memory
- 同步更新 v2/v3 的 `PROJECT_INDEX.md`；未改源码
- 备份：`.backups/20261115_crosslink/`（v3）、
  `agent-memory-router-v2/.backups/20261115_crosslink/`（v2）

### 2026-11-18 · v0.4 断点提交协议与恢复可靠性

- `checkpoint()` 改为 B 协议：**state CAS 是唯一提交点**；CAS 前不写 memory，
  CAS 后才写 journal 与归档。CAS 失败方不再产生 orphan 断点（Target 2 关闭）
- `TaskState` 新增 `schema_version / task_epoch / checkpoint_key / commit_seq /
  archive_pending / extra`；`archive_pending` 与 CAS 同写，pending 时硬停写
- 新增 `.longrun/checkpoint_journal.json` 单槽 journal；唯一 tmp + `os.replace`；
  删除前比较 `task_epoch + commit_seq`；journal 领先默认 `StateBehindJournal`
- 新增 `StatePendingError`、`CheckpointArchiveError`、`PendingArchiveError`、
  `ArchivePayloadMissing`、`AmbiguousRecovery`、`ActiveTaskExists`、
  `StateBehindJournal`、`LegacyPendingCheckpoint`
- 新增 `retry_pending_checkpoint()` / `retry_legacy_checkpoint()`；
  `resume()` 按 archive → journal → 降级重渲染修复 pending；
  `strict_payload=True` 时原文缺失 fail closed
- `recover_from_memory()` 重写候选规则：活动任务保护、终态过滤、同任务最高
  `commit_seq`、不同 epoch 歧义、`allow_legacy`、raw 缺失报错
- `require_writable()` stale lease 先落盘收回再拒绝；`status()` 改为读盘；
  `save_decision()` pending 拒绝并按 `checkpoint_key` 兜底关联
- marker 安全：只接受唯一合法 marker；用户文本与 marker JSON 转义；
  `checkpoint_id` 不再进入 marker，权威指针改为 `checkpoint_key + commit_seq`
- `InMemoryBackend.read_raw()` 在原文缺失时抛 `MemoryBackendError`，不再泄漏
  `KeyError`
- 新增 `tests/test_v4_protocol.py`、`tests/test_v4_recovery.py`，重写
  `tests/test_target2_orphan_checkpoint.py`；107 collected，
  router 缺失时 1 个条件 skip
- 更新 `CLAUDE.md`、`.cursorrules`、rules、skill、README（中英）、
  `CHANGELOG.md`、`RELEASE_NOTES_v0.4.0.md`、`pyproject.toml`（0.4.0）；
  备份：`.backups/20261118_v4/`；问题记录：`BUG_DESCRIPTION.md`

### 2026-11-18 · v0.4.1 发布收尾（lint 与测试口径）

- `ruff check .` 修复 2 项风格问题，提交协议、恢复顺序、journal 语义与
  公开 API 均不变：
  - `state_machine.py` journal 覆盖检查的 SIM102 嵌套 `if` 合并条件
  - `workflow_manager.py` best-effort journal 写入改用
    `contextlib.suppress(Exception)`（SIM105）
- 修正测试口径：总收集 107 项；`tests/test_cli.py` 仅在
  `agent-memory-router` 不可导入时 skip。README（中英）、`CHANGELOG.md`、
  `RELEASE_NOTES_v0.4.0.md`、`PROJECT_INDEX.md` 已同步
- 新增 `RELEASE_NOTES_v0.4.1.md`；版本号 `0.4.1`
- 备份：`.backups/20261118_v4.1/`；问题记录：`BUG_DESCRIPTION.md`

## 8. 技术债务

| 级别 | 项 | 说明 |
|------|----|------|
| 中 | MCP 每次调用拉子进程 | 高频检索需长连接或 Local 后端 |
| 中 | 关键词提取为启发式 | 无向量/语义召回，长尾同义词会漏 |
| 中 | `LocalMemoryBackend` 项目根是模块全局 | 同进程不能并行切项目；生产走多个 MCP 子进程 |
| 低 | 同步 API | 异步场景需线程池包装 |
| 低 | v4 持久性只到进程崩溃 | state/journal 未 fsync；掉电一致性不在承诺内 |
| 低 | `mcp` 版本上限 `<3.0` | 升级需回归 stdio 调用 |
| 低 | 宿主需调用 `begin_turn()` | 框架看不到用户消息边界；漏调会让租约跨轮存活 |
| 低 | `resume()` 自带 `begin_turn` | 宿主重复调用会多推进一轮（无害但审计不准确） |

## 9. 验证命令

```bash
python -m pytest tests/ -q        # 107 passed（router 已安装）；缺失时 106 + 1 skip
python -m cli demo
python examples/01_interrupted_task.py
python examples/01_interrupted_task.py --mcp
python examples/02_phase_archive.py
python examples/02_phase_archive.py --mcp
python -m ruff check .
```

验收时额外确认：

- 临时项目目录下出现 `memory.db`、`raw_data/`、`.longrun/`
- 路由器仓库的 `data/` 不再被写入
- MCP 模式下 `MCPMemoryBackend._server_args()` 含 `--project_path`
