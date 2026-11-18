# 02 · 铁律二：先提交并归档，再输出断点

> **在完成一个执行阶段或触发中断（如 token 预警）时，必须先完成 state 提交与
> `save_memory` 归档，归档成功后才输出断点信息。**
> 顺序不能反。断点文本是「已经提交并归档」的回执，不是「准备去存」的草稿。

断点必须写进**当前项目**的记忆库（`<项目根>/memory.db`），
热状态写到 `<项目根>/.longrun/state.json`，待归档凭证写到
`<项目根>/.longrun/checkpoint_journal.json`。三者共用同一个 `root`。

## 触发时机

- token / 上下文即将耗尽
- 一个阶段完成，准备进入下一阶段
- 等待用户确认、等待外部账号/权限
- 会话即将切换、换机器
- 任务正式完成
- 遇到无法自行解除的阻塞

## 提交点与固定顺序

**state CAS 是唯一提交点。** CAS 之前不写 memory，因此失败方不会留下
orphan 断点。

```text
1. 锁内读 state → 副本上收回租约、改状态、生成 checkpoint_key/commit_seq
2. 渲染断点正文 + 单行机器标记（archive_pending 归一化为 False）
3. state CAS（同时写入 archive_pending=True）   ← 唯一 commit point
4. 写 checkpoint_journal.json（单槽，唯一 tmp）
5. save_memory 归档（title/keywords 必须包含完整 checkpoint_key）
6. 归档成功 → 锁内清 archive_pending、记录 checkpoint_id → 删 journal
7. 才允许把断点文本输出给用户
```

`checkpoint()` 已封装上述流程，调用方不要手工拆开。

## 失败矩阵

| 失败点 | 结果 |
|--------|------|
| CAS 冲突 / 写 state 失败 | 不写 memory、不写 journal、不输出断点；抛 `StateConflict` |
| CAS 成功、journal 写失败 | journal 是 best-effort；state 仍 pending，archive 照常尝试 |
| CAS 成功、archive 失败 | state 保持 `archive_pending=True`；抛 `CheckpointArchiveError`，不输出断点 |
| archive 成功、清 pending 失败 | 断点文本可以输出，但结果标记 `archive_pending=True` |
| 进程崩溃（CAS 后、archive 前） | `resume()` 从 state + journal 补档 |
| 进程崩溃（archive 后、清 pending 前） | `resume()` 按 `checkpoint_key` 找到 archive 并清 pending |

补档入口：

- `resume()`：修复 pending 后继续任务，并开启新轮次
- `retry_pending_checkpoint()`：只补归档、清 pending，不改状态/轮次
- `retry_legacy_checkpoint()`：显式处理 v3 的 `pending_checkpoint.json`

修复顺序固定为 **archive → journal → 按 state 降级重渲染**。
`strict_payload=True` 时，原文缺失直接抛 `ArchivePayloadMissing`，不降级。

## 断点必须包含

| 字段 | 说明 |
|------|------|
| 任务 | 一句话描述 + task_id |
| 类型 | `interrupt` / `phase_complete` / `blocked` / `complete` |
| 状态 | 状态机当前状态、读写门禁 |
| 阶段 | 当前阶段名 |
| 已完成 | 本阶段做完了什么（可验证的结果，不是「在做了」） |
| 关键决策 | 为什么这么选，排除了什么 |
| 下一步 | 恢复后第一件事，必须具体到可执行 |
| 机器标记 | 单行 `<!-- longrun-state: {...} -->` |

## 机器可读标记

断点原文最后一行必须是恰好一个 marker：

```html
<!-- longrun-state: {"task_id":"T_...","schema_version":4,"task_epoch":"...","checkpoint_key":"...","commit_seq":2,"archive_pending":false,...} -->
```

它是「换机器后只靠记忆重建状态」的唯一依据。规则：

- 禁止省略，禁止拆成多行，禁止在标记里塞正文
- `checkpoint_key` 是权威指针，完整值必须同时出现在 title 或 keywords 中；
  检索层只匹配 `title / keywords / id`，不会搜正文
- `checkpoint_id` 只是 archive 成功后的 best-effort 缓存，不进入 marker，
  也不能作为恢复资格
- 只接受**恰好一个**合法 marker；多个 marker 一律不参与自动恢复
- 用户可控文本中的 `<!--` / `-->` 会被转义；marker JSON 内的字面量
  `<!--` / `-->` 会被编码为 `U+003C` / `U+003E`，解析后的值不变
- v3 旧标记没有 `task_epoch/checkpoint_key/commit_seq`，默认 `unverified`；
  需要显式 `allow_legacy=True` 才参与恢复

v4 起 `checkpoint()` 会先收回单轮写租约再渲染标记，所以这里正常就是
`read_only`。恢复后要继续写，必须在新的轮次重新 `enable_writes(原因)`。

## 耐久性承诺

v4 只承诺**进程崩溃**级别的恢复：state/journal 的唯一 tmp + `os.replace`、
单槽 journal、`resume()` 对账。

本轮不承诺掉电级持久性：state/journal 的 `fsync`、`raw.txt` 的 `fsync`、
SQLite `synchronous=FULL` 都不在 v4 范围内。

## 反模式

- ❌ 先打印断点，再调用 `save_memory`
- ❌ 在 state CAS 之前写 memory（CAS 失败就会留下 orphan）
- ❌ 归档失败仍然输出断点，让用户误以为已保存
- ❌ pending 未清就继续写文件或写新断点
- ❌ 只写「继续做」三个字，没有下一步
- ❌ 断点只存在聊天记录里，没有可检索的 `checkpoint_key`
