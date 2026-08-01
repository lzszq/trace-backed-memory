# ADR-0006：Full Persistence 与 Reducer-Native Memory

**状态：** 已接受
**日期：** 2026-08-01
**English:** [0006-full-persistence-reducer-native-memory.md](0006-full-persistence-reducer-native-memory.md)

## 背景

仓库目前有两条成熟运行路径：version-2 兼容 Store，以及显式 durable-v3 authority
graph。durable graph 已能让 authorization、GateSession、retrieval、Semantic Gate、
finalization、execution、outcome、outbox 与 replay 状态跨重启保留，但这些记录仍分散
在彼此独立的 authority 中。系统尚无一份 canonical event history，可由 versioned
reducer 重建全部受支持 projection。

现有若干组件虽然是 append-only 或 content-addressed，但没有任何一个是完整系统
ledger。尤其是 `tbm.audit-event.v3`，它是 audit evidence，而不是 lifecycle 的事实
源。继续增加隔离 authority 只会提高迁移与一致性成本，不能补齐这一架构缺口。

## 决策

- canonical append-only event ledger 是 domain、lifecycle、control 与 effect 状态的
  最终事实源。
- 已认证、content-addressed Artifact Authority 是大对象或受保护字节的事实源。
  event 只引用 artifact，不嵌入无界或敏感 payload。
- versioned deterministic reducer 消费 canonical event 并构建可替换 projection。
  projection 可以删除后重建，不是独立事实源。
- 现有兼容与 durable-v3 authority 在 event-first cutover 完成前继续作为受支持迁移
  资产。既有记录必须经过 import、shadow compare 和保留，不能直接改名为 canonical
  ledger。
- cutover 期间，event append 与所有关键 projection update 必须使用同一 database
  connection、unit of work 和 transaction。禁止独立、best-effort dual write。
- review、structured verification、authorization、System Gate 单调性、Semantic Gate
  收窄、finalization 与 exact replay 仍是产品不变量。reducer 只重建 decision，不能
  削弱它们。
- Git 是 observation/evidence provider。Git history 不是系统 ledger，不能替代已保留
  的 execution、authorization、Gate 或 effect evidence。
- external effect 使用显式 intent、attempt 与幂等 receipt event。不能把 database
  transaction 描述为与外部 side effect 原子提交。
- 冻结新增独立 authority、protocol family 或 standalone SQL component；只有已记录的
  security/corruption 修复，或 ledger、reducer、migration cutover blocker 可以例外。
- snapshot version 2、SQLite schema version 1、PostgreSQL schema version 2、
  `tbm.agent.v1` 与显式 durable-v3 profile 在另行核验的 cutover 前保持兼容边界。

## 影响

本 ADR 改变架构优先级，不改变当前运行行为。因此机器可读能力状态继续报告
`persistence_model="authority_graph"` 与 `full_persistence=false`。

交付按 F0-F6 release train 增量推进：event contract 与 ledger port、
SQLite/PostgreSQL ledger、reducer runtime 与 rebuild tooling、event-first lifecycle
adapter、legacy import 与 shadow comparison，最后完成 shared-service 与 stable-release
资格验证。rollback 选择旧 reducer/projection head，不删除 canonical event。

## 退出证据

只有完整 Definition of Done 全部满足后，才能声明 Full Persistence：canonical event
append 与 artifact linkage、跨版本 deterministic rebuild、event-first 本地与共享
transport、已核验 legacy import/cutover、authorization 与 Gate 不变量、幂等外部 effect
receipt、backup/restore 与 disaster-recovery 证据、retention/crypto-erasure，以及生产级
observability 与 governance。在此之前，所有已交付增量都保持
`full_persistence=false`。
