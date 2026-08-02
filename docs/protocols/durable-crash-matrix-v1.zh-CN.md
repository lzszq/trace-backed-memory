# Durable SQLite crash matrix v1

[English](durable-crash-matrix-v1.md) | **简体中文**

F2-08 使用硬进程终止来核验显式 event-first SQLite 生命周期。harness 会针对真实的
file-backed SQLite v3 数据库启动子进程，并在每个语义 commit point 调用
`os._exit()`。故障 hook 仅存在于测试子进程；生产依赖、公开 request JSON 与
`tbm.durable-agent-wire.v1` 均未改变。

## 固定矩阵

十一个 commit point 为：

1. authorization decision；
2. `CREATED`；
3. retrieval evidence；
4. `PREPARED`；
5. provider call；
6. `DECIDED`；
7. replay retention；
8. `FINALIZED`；
9. `EXECUTING`；
10. outcome；
11. outbox event 与初始 delivery。

每个点运行两种模式。pre-commit kill 在选定 write/callback 之后、event-first 命令
guard commit 之前发生。重新打开数据库后必须通过 `PRAGMA integrity_check`，并在每个
受管表上完整重现故障前数据库快照；重新发出命令后必须精确到达预期状态。

response-loss kill 会先让真实命令 guard 完成 commit，再在 response 到达 caller 前
退出子进程。重新打开后必须看见已提交 session 和事件投影；使用原始 expected version
重新发出原始 request 后，GateSession、事件序列、事件哈希与聚合 projection digest
必须完全不变，不得产生第二个逻辑 transition。

这些断言共同证明 F2-08 的四项要求：

- 已确认的规范事件不会丢失；
- 精确重试不会创建重复逻辑 transition；
- recovery 使用已保留的 identity、bytes、version 与 digest；
- 每次 crash 的结果只能是精确的旧 prefix 或精确的已提交结果，绝不留下未知 partial
  database state。

## Provider 与 outbox 边界

durable wire 从可信 adapter 接受精确 provider response bytes。provider callback 返回后
发生 pre-commit kill 时，重试可能再次调用 callback，但不会留下重复的持久化 attempt
或 decision。已保留的成功 decision 会直接 replay，不会再次调用 provider。这不是
provider exactly-once 保证。

completion 会原子包含 RunOutcome、completed GateSession、Outcome/Effect 事件、outbox
event 与初始 pending delivery。外部 consumer 调用仍位于数据库事务之外；delivery
继续是 at-least-once，通过不可变 `event_id` 去重，并在 acknowledgement write 不确定时
依赖 lease reclaim 与显式 `recovery_required`/`superseded` 分类。

## 当前限定

该矩阵核验 `tbmd local` 与独立 SQLite durable HTTP/MCP/SDK profile 使用的显式本地
SQLite event-first 组合。独立 PostgreSQL 命令协调器与 Outcome/Effect 投影尚未完成
同等切换或 crash-matrix 运行。F2-08 在其固定十五个验收点下已经完成，但 F2 exit gate
与 Full Persistence 尚未完成：F2-03、PostgreSQL parity、其余 reducer cutover 与后续
release-train gate 仍未关闭，因此 `full_persistence` 继续为 `false`。

可执行证据位于 `tests/test_durable_crash_matrix.py` 与
`tests/durable_crash_matrix_child.py`。
