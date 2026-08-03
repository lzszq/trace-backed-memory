# Reducer 与 Projection Runtime v1

[English](reducer-v1.md) | **简体中文**

`tbm.reducer.v1` 是 versioned deterministic reducer 与可重建 projection 的
opt-in、存储中立执行契约。它可通过 package API 以及显式的 `tbmd ledger ...` / `tbmd
projection ...` operator 命令使用。默认 Agent、durable lifecycle、HTTP、MCP 与 SDK
composition 都没有选择它，因此当前持久化模型仍是 `authority_graph`，且
`full_persistence=false`。

## Reducer descriptor 与 registry

每个 reducer 都有 immutable `ReducerDescriptor`，绑定：

- reducer ID 与正整数版本；
- 排序后的输入 event type，或显式 envelope-only 通配符；
- 输出 projection 名称与 schema version；
- reducer code 与 configuration 的 SHA-256 摘要；
- 可选 typed-event 目标版本；
- `deterministic=true`。

`ReducerRegistry` 会拒绝重复 reducer version 和 projection/version ownership，在
`seal()` 后不可变，可解析显式或最新版本，并核验预期 code/configuration 摘要。其
catalog 具有 canonical registry digest。typed reducer 通过 sealed
`EventTypeRegistry` 消费 event；未知 type、未知 version 或缺失 upcaster 都会阻塞
rebuild。内置 `canonical-event-inventory` 是 envelope-only 诊断 projection：它只统计
canonical event type，不读取 payload 内容。

## 纯函数与确定性执行

Projection state 是有界 canonical JSON：根必须是 object，只允许 signed 64-bit
integer、string、boolean、null、array 与 object。float、非 string key、不支持的对象、
超出 depth/node 限制或超过 1 MiB 的 state 都会被拒绝。每个 initial state 与 transition
都会从两份独立 immutable state view 执行两次；canonical projection digest 不同会以
稳定的 nondeterminism error 失败。此 runtime 不向 reducer 提供 clock、filesystem、
network、random source、model、provider 或可变 source event。

canonical projection digest 使用 domain separation，并绑定 projection 名称、输出 schema
version 与完整 state；它不包含当前 Python version、操作系统、路径、locale、clock 或
换行符。

## Checkpoint 与 rebuild

`ProjectionCheckpoint` 绑定精确 reducer descriptor、tenant partition、最后扫描的 global
position、ledger high-watermark、canonical state digest、reducer/event registry digest、
owner、创建时间与 rebuild generation。content-addressed build ID 不会使 checkpoint
成为事实源；对该 opt-in ledger 而言，canonical event 仍然 immutable。

`ProjectionRuntime.rebuild()` 要求所选 partition 具备覆盖全部 classification 的 access
view，冻结第一次观察到的 ledger high-watermark，按顺序读取有界 global page，并保存
周期 checkpoint 与最终 checkpoint。`resume=True` 只接受 reducer、code、configuration、
output schema 与 registry digest 全部精确匹配的 checkpoint。checkpoint 超前于 ledger 或
descriptor 不匹配时会 fail closed。若 store 返回了属于另一个 reducer ID 或 version 的
checkpoint，runtime 会在执行任何 retained state 前以
`TBM_REDUCER_VERSION_MISMATCH` 拒绝。

失败 event 绝不会被跳过。结果为 `blocked`，并携带 `ProjectionBlocked` evidence：event
与 payload digest、reducer version、projection、last good position、稳定 error code、
retryability 以及所需 upcaster/migration 提示。修复必须使用新 reducer、upcaster、
compensating event 或已批准 migration，不能修改 source event。

## Shadow compare、activation 与 rollback

`compare()` 比较同一 projection 与 partition 的两个 build，绑定完整 state/global-position
digest，并最多输出 256 条 path-level difference；差异只含 value digest，不暴露
projection 内容。

Activation 必须显式批准。append-only activation chain 使用 expected head version/current
build CAS 与精确读回。替换已有 head 时，必须提供绑定当前 active build 与 shadow build
的 comparison。Rollback 追加另一条 head selection，指向曾经 active 且仍保留的 build；
它绝不会删除 event 或 checkpoint。

SQLite 在 event-ledger schema 内、同一 owner lock 与 transaction machinery 下保存
immutable checkpoint 和 append-only activation record。PostgreSQL 以固定 schema lock、
row-locked head selection、caller savepoint、精确 catalog verification 与 fail-closed
`RESTRICT` rollback 实现同一 repository protocol。

## Operator 命令

operator 命令面向一份显式、既有的 SQLite event-ledger 文件，不会隐式复用
`.tbm/durable.sqlite3`。数据库保留多个 partition 时，必须传入
`--partition-sha256`。

```text
tbmd ledger verify --database .tbm/event-ledger.sqlite3
tbmd ledger stats --database .tbm/event-ledger.sqlite3
tbmd projection list --database .tbm/event-ledger.sqlite3
tbmd projection rebuild --database .tbm/event-ledger.sqlite3 --generation 1
tbmd projection compare --database .tbm/event-ledger.sqlite3 ACTIVE SHADOW
tbmd projection activate --database .tbm/event-ledger.sqlite3 SHADOW --approve
tbmd projection rollback --database .tbm/event-ledger.sqlite3 PROJECTION
```

每条命令只输出一个 canonical、deterministic JSON 值。命令会持有 ledger owner lock，
发现并核验精确 partition identity，使用完整 classification view，返回 metadata/digest 而
不是 raw event payload，并保留稳定 public error。`projection activate` 与 `rollback`
还接受可选 expected-head 参数，作为 storage CAS 之外的 operator precondition。

## 跨平台确定性证据

`tests/fixtures/event_projection_v1.golden.json` 把公开 canonical event fixture 绑定到
inventory reducer descriptor、initial-state digest、完整 projection 与 projection digest。
预期摘要为：

```text
sha256:9a257f398b55db473403a66d17cafc01983baa50aeb68ca70d69783c0444e9d4
```

`tools/verify_projection_determinism.py` 已进入仓库 verification gate。CI 会在 Windows
与 Linux 的 Python 3.11、3.12、3.13 上运行同一份 committed golden fixture。各平台
不会自行生成 expected value；六个 job 都必须匹配同一份字节。

这项 F1 能力证明 generic runtime 与 operator lifecycle。第一组 F2 增量现已注册 pure
GateSession-current 与 Gate-evidence-current reducer。canonical GateSession revision
events 可重建精确 current session，并与保留的 authority rows 逐字段 parity；SQLite
还会为该 reducer 持久保存 checkpoint/resume、shadow comparison、activation 与 rollback。
Retrieval 与 System Gate events 可重建紧凑的 current evidence 和 Artifact linkage。详见
[Gate Evidence Event v1](gate-evidence-event-v1.zh-CN.md)。后续已交付的 F2 增量要求读取
已保留的 System Gate parent event，并重建精确 Semantic attempt chain 与 descriptor-only prompt/
response linkage；parity 会同时绑定 authority bundles 与 canonical event bytes。详见
[Semantic Gate Attempt Event v1](semantic-gate-attempt-event-v1.zh-CN.md)。当前 F2 slice
注册 `final-decision-injection`；它消费
`tbm.usage_decision.finalized` 与 `tbm.injection.rendered`，重建精确 decision/
injection/manifest/Artifact-role head，并与保留的 GateSession/replay authority 做逐字段
parity。详见 [Finalization Event v1](finalization-event-v1.zh-CN.md)。默认 registry
还包含 `outcome-current`、`outcome-attribution` 与 `effect-queue`；显式
SQLite/PostgreSQL event-first 路径会核验 RunOutcome、OutcomeAttribution、completion
delivery history 与 dead-letter parity。详见
[Effect Event v1](effect-event-v1.zh-CN.md)。MemoryCatalog、activated head、retrieval
index、audit、metrics 与 PR risk 尚未 reducer-native。存储中立 `effect-queue` version 2
现会重建 provider receipt、unknown-result、reconciliation 与显式 retry 状态。active
provider callback、provider-specific reconciliation、durable compensation 与 F2 其余
cutover gate 仍未关闭，因此这些 opt-in reducer 不会改变 `full_persistence=false`。
