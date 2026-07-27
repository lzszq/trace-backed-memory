# Snapshot v3 迁移预检

[English](snapshot-v3-preflight.md) | **简体中文**

本文说明已经交付的只读 schema version 3 协同迁移预检。它不改变当前兼容边界：
runtime snapshot 仍为 version 2，SQLite 仍为 schema version 1，PostgreSQL
仍为 schema version 2，Agent 协议仍为 `tbm.agent.v1`。

## 命令

```text
tbm migration plan-v3 SNAPSHOT_V2 MAPPING_JSON \
  [--repository-root REPOSITORY_ID=PATH]...
```

该命令要求输入显式包含 `snapshot_version: 2`，先通过
`TraceBackedMemoryStore` 验证 version-2 snapshot，再解析封闭、有界的
operator mapping，最后输出一份确定性 JSON 计划。它不会改写 snapshot 或 mapping
文件。只要输入语法有效，即使 `ready` 为 false，命令也会成功返回；调用方必须检查
`ready`、`counts.errors` 与稳定 issue code。

当 mapping 选择 `ancestry_policy.mode="required"` 时，regression evidence
使用的每个 repository 都必须通过可重复的 `--repository-root` 绑定到可信本地 Git
对象库。命令使用 `git merge-base --is-ancestor` 验证 source-to-fix 与
fix-to-regression 两条关系；对象缺失、关系不成立或 Git 不可用都会在计划中
fail closed。命令不会从当前工作目录推断 checkout。

对应 Python API：

```python
from trace_backed_memory import (
    parse_v3_migration_mapping,
    plan_snapshot_v3_migration,
)

mapping = parse_v3_migration_mapping(mapping_payload)
plan = plan_snapshot_v3_migration(
    snapshot_payload,
    mapping,
    commit_relation_verifier=trusted_commit_relation_verifier,
)
assert plan.ready
```

`trusted_commit_relation_verifier(repository_id, relation)` 是应用层端口，
必须认证 repository evidence，并返回严格的 `bool`。`required` policy 下省略
该端口会产生 `TBM_V3_ANCESTRY_VERIFIER_REQUIRED`；异常、非布尔结果以及被拒绝的
关系都会成为确定性的迁移错误。

## 显式 operator mapping

Mapping 协议为 `tbm.snapshot.v2-to-v3.mapping.v1`，它要求：

- canonical repository registry，包含 provider identity、canonical locator
  hash 与显式 legacy alias；
- canonical tenant registry 与显式 legacy alias；
- 每条 Trace 都有 repository/tenant binding；
- 每条 Lesson 与 Project Policy 都有 `global`、`tenant` 或 `repository`
  authorization scope，并与 applicability attributes 分离；
- 每个 verified legacy Failure Case 都有结构化 regression evidence，包括
  passing regression Trace/run/evaluator，以及已验证的 source-to-fix 与
  fix-to-regression ancestor relation（commit 是其自身的 ancestor）；
- 每条 global Project Policy 都有 privileged approval evidence；
- ancestry policy 必须为 `required`，或为带非空审计 bypass reason 的
  `disabled`。

`disabled` 不会静默生效：计划会包含 `TBM_V3_ANCESTRY_DISABLED`。Mapping 中的
repository locator hash、evaluator identity、artifact digest 与 global-policy
approval record 都是迁移规划所用的 operator declaration，并不是 authorization
或 attestation service。预检 ready 不会激活迁移后的 memory；未来 writer 必须把
这些 declaration 绑定到认证 registry，并让证明不足的 legacy record 保持受限
trust state。

缺失的 `repo` 或 `tenant` 永远不会被解释为 global access。缺省 legacy scope
字段也不会被升级为 global scope。Mapping 可以收窄 applicability，但不能移除既有
repository、tenant 或 metadata 约束。

## Report 语义

Plan 协议为 `tbm.snapshot.v2-to-v3.plan.v1`。报告使用带算法标记的 SHA-256，
绑定显式 source snapshot 与 canonical mapping。Registry array、alias、
applicability attribute 与 artifact digest 会在 mapping hash 前归一化，因此语义
等价的 operator mapping 会得到相同 digest。Issue 按确定性顺序输出，并分为：

- `error`：迁移尚未 ready；
- `warning`：后续可以迁移，但限制必须继续显式保留。

既有 version-2 usage log 会产生 `TBM_V3_LEGACY_REPLAY_PARTIAL`。该 warning
不会伪造 version 2 从未保存的 retriever、model、prompt、ancestry、renderer、
response 或 snippet evidence。

## 机器可读资源

- `schemas/snapshot_v3_migration_mapping.schema.json`
- `schemas/snapshot_v3_migration_plan.schema.json`
- `examples/snapshot_v3_migration_mapping.example.json`
- `examples/snapshot_v3_migration_plan.example.json`

四项均为 canonical packaged resource，并在仓库、wheel 与 source distribution
之间保持逐字节一致。

## 刻意保留的边界

预检不会生成或加载 version-3 snapshot，不会修改 SQLite/PostgreSQL，不会持久化
Gate session，也不会宣称 complete decision replay。上述能力必须等待剩余的
version-3 domain、persistence、transaction、rollback 与 recovery 实现协同完成。
保持预检只读，可以防止不完整 schema 意外成为 production format。
