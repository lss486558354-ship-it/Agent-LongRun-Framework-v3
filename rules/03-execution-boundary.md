# 03 · 执行边界：状态机 + 读写门禁

> 状态机负责「能不能从这里到那里」，门禁负责「现在能不能动文件」。
> 两者都不允许被自然语言绕过。

## 项目根

`LongRunAgent(root=项目根)` 同时决定：

- `.longrun/state.json` 的位置（热状态）
- `.longrun/checkpoint_journal.json` 的位置（待归档凭证）
- 默认 MCP 后端的 `--project_path`（冷记忆）

两者必须指向同一个项目。`resume()` 跨项目读取会得到错误的 `next_step`，
且大概率在错误的记忆库里找不到断点。

## 状态文件并发

同一个项目根可能被多个进程同时打开（多个终端、多个 MCP 子进程）。
`state.json` 的读-改-写必须经过 `.longrun/state.lock` 跨进程锁：

- 每次保存写唯一临时文件，再 `os.replace`；不共用固定 `.tmp`
- `TaskState.revision` 每次成功保存递增；`save(expected_revision=...)`
  不匹配时抛 `StateConflict`
- `task_id` / `task_epoch` 也是 CAS 条件；防止 revision 相同但任务已替换的 ABA
- journal 是单槽：已存在更新的 checkpoint 时拒绝覆盖；删除前比较
  `task_epoch` + `commit_seq`，不能误删别的断点
- `status()` 默认读磁盘；缓存不能成为门禁与查询之间的分叉
- 遇到 `StateConflict` 不要直接重试覆盖；先 `resume()` 对齐状态
- 锁文件永不删除；删除会让并发进程各持不同 inode 的锁

## 状态机

```text
pending ──▶ in_progress ──▶ paused ──▶ in_progress
                 │            ▲
                 ├──▶ blocked ┘
                 ├──▶ waiting_verification ──▶ completed
                 ├──▶ completed（终态）
                 └──▶ failed（终态）
```

| 当前状态 | 允许流向 |
|----------|----------|
| `pending` | `in_progress` |
| `in_progress` | `paused` / `blocked` / `waiting_verification` / `completed` / `failed` |
| `paused` | `in_progress` / `failed` |
| `blocked` | `in_progress` / `failed` |
| `waiting_verification` | `in_progress` / `completed` / `failed` |
| `completed` / `failed` | 无出口 |

**非法流转必须抛 `TransitionError`，不允许「大概齐」跳过去。**

其它硬约束：

- 已有未完成任务时，禁止 `start_task` 覆盖；先 `resume()` 或 `finish()`
- state 处于 `archive_pending=True` 时，禁止 `start_task` / 新 `checkpoint` /
  `enable_writes` / `require_writable` / `save_decision`
- 终态任务禁止再写断点
- 状态文件损坏必须报错，不能静默当成「没有任务」
- `start_task()` 永远以 `read_only` 启动；`access_mode` 不是公开参数，
  旧调用会直接抛 `LongRunError`（不存在「启动即写」的合法路径）
- `transition()` 进入任何新状态都会收回写租约；跨状态不继承写权限
- `from_dict()` / `__post_init__()` 会把非 `in_progress` 状态的 writable
  租约归一化为 `read_only`
- `from_dict()` 遇到高于当前支持的 `schema_version` 必须拒绝，不能静默
  丢掉新字段；未知字段通过 `extra` round-trip 保留

## 读写门禁（单轮写租约）

| 模式 | 允许 | 禁止 |
|------|------|------|
| `read_only`（默认） | 读代码、检索记忆、跑只读分析 | 改源码、删文件、`git commit`、外部副作用 |
| `writable`（仅当前轮） | 上述 + 修改/创建/删除、提交 | 越过用户明确边界的高风险操作；跨轮次继续写 |

规则：

1. 新任务一律从 `read_only` 开始；`start_task()` 不接受 `access_mode`
2. 调研完成、方案确认后，调用 `enable_writes("原因")` 解锁**当前轮**；
   只有 `in_progress` 状态可以解锁
3. 每次写操作前调用 `require_writable("动作")`
4. 回到调研/等待确认时，调用 `disable_writes("原因")` 重新上锁
5. `enable_writes` 必须带非空原因，原因会写进 `history`，便于事后审计
6. 门禁是基于「当前任务状态」的，不是基于「用户的某句话」的；
   用户说「动手吧」之后仍应先更新状态，再执行写操作
7. `begin_turn()` / `end_turn()` / `checkpoint()` / `resume()` /
   `recover_from_memory()` 都会收回写权限；一条批准只覆盖一轮
8. 宿主必须在每条新用户消息开始时调用 `begin_turn()`；
   `resume()` 已自带一次，不要重复调用
9. `require_writable()` 同时校验 `status is in_progress`、`access_mode`
   与 `write_lease_turn`；过期租约即使模式仍是 `writable` 也必须拒绝。
   拒绝前先把 `read_only` / `write_lease_turn=None` 落盘，不能只改内存
10. `paused` / `blocked` / `waiting_verification` / 终态都不是可写状态；
    `transition()` 会自动收回租约，恢复后必须重新 `enable_writes(原因)`
11. `archive_pending=True` 是硬停写状态：先 `resume()` 或
    `retry_pending_checkpoint()` 补档，再重新授权
12. `begin_turn` / `end_turn` / `status` / `disable_writes` 在 pending 时仍可用；
    它们不修复断点，也不会吞掉 pending

## 哪些算写操作

- 修改/创建/删除仓库文件
- `git add / commit / push`
- 安装依赖、改配置
- 发外部请求产生副作用（下单、提交表单、发消息）
- 任何不可逆操作

以下不算：读取文件、`search_memory`、`read_raw_data`、只读查询、`git status`。

## 恢复边界

权威顺序固定为 **state → journal → memory**：

- `.longrun/state.json` 是热状态，也是任务状态的唯一权威
- `state.json` 丢失但 journal 存在：`resume()` 用 journal 重建，并保留
  journal 原文用于补档；重建后的 `revision` 不得小于 `commit_seq`
- journal 领先 state：默认抛 `StateBehindJournal`；确认可信后
  `resume(adopt_journal=True)` 才采纳
- state 与 journal 都没有原文：非 strict 模式按 state 降级重渲染；
  `strict_payload=True` 抛 `ArchivePayloadMissing`
- `recover_from_memory()` 只在本地没有可保护的任务时使用：
  - 本地有活动任务：默认抛 `ActiveTaskExists`；同一任务且显式
    `force=True` 才允许
  - 本地是终态：默认也拒绝；`force=True` 才允许
  - 终态候选默认跳过；`include_terminal=True` 才纳入
  - 同一任务取最高 `commit_seq`；不同 `task_epoch` 默认
    `AmbiguousRecovery`
  - v3 旧标记默认 `unverified`；需要 `allow_legacy=True`
  - 索引命中但 `read_raw` 失败：抛 `ArchivePayloadMissing`，不静默跳过
- `recover_from_memory()` / `resume()` 都强制回到 `read_only` 并开启新轮次；
  断点标记里的 `access_mode` 只用于审计，不再恢复写权限
- 恢复后要继续写，必须在当前轮重新调用 `enable_writes(原因)`

## v3 兼容

- v4 不再写 `.longrun/pending_checkpoint.json`
- 检测到 v3 fallback 时，`resume()` / `start_task()` / `checkpoint()` 会抛
  `LegacyPendingCheckpoint`
- 显式处理：`retry_legacy_checkpoint()` 重新渲染并归档；或
  `clear(force=True)` 明确丢弃
- 旧 state 没有 `task_epoch/checkpoint_key/commit_seq` 仍可加载；下一次
  v4 断点会补上这些字段
