# PROJECT_INDEX

> 最后更新：2026-11-15（v2/v3 交叉链接 + v2 Topics）
> 维护规则：每次代码修改后追加「修改历史」，并刷新此处。

## 1. 项目基本信息

| 项 | 内容 |
|----|------|
| 名称 | Agent-LongRun-Framework |
| 版本 | 0.3.0 |
| 定位 | 把 Agent 执行状态机与项目级 MCP 记忆路由耦合的长任务工作流；写权限为单轮租约 |
| 语言 | Python 3.10+（开发环境 3.12.0） |
| 依赖 | `mcp>=2.0,<3.0`；开发依赖 pytest、ruff |
| 入口 | `workflow_manager.LongRunAgent`；CLI `agent-longrun demo` |
| 上游 | `engineering-workflow`（状态语义/断点格式）、`agent-memory-router-v2`（项目级 MCP 记忆） |
| 远端 | https://github.com/lss486558354-ship-it/Agent-LongRun-Framework-v3 |
| License | MIT |
| 项目数据 | `<项目根>/.longrun/state.json`、`<项目根>/memory.db`、`<项目根>/raw_data/` |

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
  `turn_index`、`write_lease_turn`、`revision`）
- `StateStore(root, lock_timeout=10.0)`：`<root>/.longrun/state.json` 热状态；
  `.longrun/state.lock` 跨进程锁；唯一 `state.<pid>.<uuid>.json.tmp` +
  `os.replace`；`revision/CAS` 防丢失更新；损坏抛 `StateCorruptError`，
  冲突抛 `StateConflict`，锁超时抛 `StateLockTimeout`
- `load()` 在文件存在时也持锁，避免 Windows `os.replace` 与打开中的句柄冲突
- `transition()` 校验流转、记录 history（最多 20 条），并强制收回写租约；
  `__post_init__()` / `from_dict()` 归一化非 `in_progress` 状态的 writable 租约

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
- `checkpoint()`：锁内快照 → 收回写租约 → 副本上改状态 → 渲染（含
  `<!-- longrun-state: {...} -->`）→ 锁外 `save_memory` → 锁内按
  `base_revision` CAS 提交状态与 `checkpoint_id` → 返回；期间被改抛
  `StateConflict`，不覆盖
- 失败：写 `.longrun/pending_checkpoint.json`，抛 `CheckpointWriteError`，不返回断点
- `complete_phase()`：状态保持 `in_progress`，阶段写入 `completed_steps`
- `finish()`：归档后进 `completed`
- `note_blocked()`：`blocked`
- `resume()`：锁内读本地状态 → `_start_new_turn`（收回租约、`turn_index+1`）→
  修到 `in_progress` → 锁内落盘；锁外读断点原文 → 搜索关联
- `recover_from_memory()`：搜索 session → 读原文 → 正则解析机器标记 → 重建状态
  → `_start_new_turn`（强制 `read_only`）
- 门禁：`enable_writes / disable_writes / require_writable`；
  `begin_turn / end_turn` 管理轮次边界；所有状态变更在锁内重新读盘
- 单轮写租约：`enable_writes()` 仅在 `in_progress` 且原因非空时设置
  `write_lease_turn=turn_index`；`require_writable()` 从磁盘读取最新状态，
  同时校验状态、模式与轮次，非执行状态或过期租约即拒绝
- `save_decision()`：写 `decision`，与断点建 `related_to`
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
      → Memory.save（session，正文含机器标记）
      → StateStore.save（锁内 CAS，status / checkpoint_id / turn_index / revision）
      → 返回断点
  → 新用户消息 begin_turn（turn_index+1，收回租约）
  → resume / recover_from_memory（必须使用同一个 root；强制 read_only）
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

## 8. 技术债务

| 级别 | 项 | 说明 |
|------|----|------|
| 中 | MCP 每次调用拉子进程 | 高频检索需长连接或 Local 后端 |
| 中 | 关键词提取为启发式 | 无向量/语义召回，长尾同义词会漏 |
| 中 | `LocalMemoryBackend` 项目根是模块全局 | 同进程不能并行切项目；生产走多个 MCP 子进程 |
| 低 | 同步 API | 异步场景需线程池包装 |
| 低 | 状态 schema 无版本号 | 未来字段变更需要迁移策略 |
| 低 | `mcp` 版本上限 `<3.0` | 升级需回归 stdio 调用 |
| 低 | 宿主需调用 `begin_turn()` | 框架看不到用户消息边界；漏调会让租约跨轮存活 |
| 低 | `resume()` 自带 `begin_turn` | 宿主重复调用会多推进一轮（无害但审计不准确） |

## 9. 验证命令

```bash
python -m pytest tests/ -q
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
