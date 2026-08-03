# Full Persistence 进度契约

[English](full-persistence-progress.md) | **简体中文**

机器可读事实源是
[`full-persistence-progress.json`](full-persistence-progress.json)。它冻结针对桌面执行
计划的所有进度报告分母。后续报告只有在列出受影响 atom ID 与证据时才能修改分子；
不得用某个 phase 或顶层 bullet 数替换总分母。

## 固定分母

490 个 atom 按完整计划重建如下：

| 来源 | 数量 |
| --- | ---: |
| F0-F6 release-train 顶层 bullet | 312 |
| 去重后的 test matrix | 74 |
| Definition of Done | 67 |
| Retention/erasure 要求 | 33 |
| 跨阶段 global gate | 4 |
| **总计** | **490** |

固定 phase 分母为 F0 48、F1 90、F2 62、F3 117、F4 38、F5 48、F6 87。
这些只是同一组 490 atom 的分类，不是额外工作。

## 汇报规则

正式进度只计算已提交 atom。候选进度必须单独汇报，并列出每个未提交 atom。证据未知、
测试不足或文档互相矛盾时，一律算未完成。不能根据总分子反推历史 phase 分布，因为那会
制造并不存在的证据。

当前正式基线是 182/490（37.14%）：由经审计的 162-atom 既有基线，加上已提升的
20-atom F2 event-first tranche 构成。该 tranche 覆盖 finalization/replay、
GateSession/Gate-evidence/Semantic event 与 reducer、RunOutcome/
OutcomeAttribution projection，以及本地 EffectQueue delivery-history/dead-letter
slice。机器可读契约保留上一批已提升 atom ID 与证据路径。当前没有额外未提交 atom
候选；未提交 provider-effect 批次无法映射到一个完整计划 atom，其 generic compensation
slice 也不提升。

新增的 local-daemon 子进程 hard-restart 测试覆盖已确认的 `PREPARED`、`DECIDED`、
`FINALIZED`、`EXECUTING` 与 `COMPLETED` commit，并要求精确重试和最终 reducer parity。
这只是 F2 crash matrix 的部分证据，不是已完成 atom，因此 182/490 保持不变。

新增 SQLite `SIGKILL` probe 覆盖已提交 authorization、`CREATED` 与 Gate-evidence
边界、finalization replay 和 completion/outbox transaction 内回滚，以及 consumer
返回但 ack 尚未 durable 时的 lease reclaim 与 at-least-once redelivery。精确
`CREATED` 恢复会重新授权且不改写 orphan evidence；finalization 会重建唯一确定的
claim-time bundle。commit 后 response-loss probe 现覆盖 `DECIDED`、event-first
`FINALIZED`、`EXECUTING`、组合 completion/outbox 与已提交 acknowledgement，精确
重试不会重复 replay 或 redelivery。本地 happy path 还通过真实 JSON-RPC STDIO MCP，
与 Python facade、Python HTTP sync/async SDK 及 TypeScript HTTP SDK 对齐全部 21 个
global event、八条 stream head 和九个已注册 reducer projection。配置后的显式 SQLite
runtime 现在已有 Semantic provider request/attempt/receipt/reconciliation evidence，
以及 provider 边界前后的 hard-kill probe。本批还增加 request-only 原子 claim、服务端证明
的 provider/policy-bound request、每个 original effect 仅一条 compensation、仅限支持 contract
的 receipt-backed generic compensation 与跨 transport Semantic invocation parity。配置后的
reconciliation、服务端证明的 owner fencing 与有界 retry/dead-letter 会核验精确保留 evidence。
Semantic provider effect 不支持 compensation，也不声明 remote exactly-once。completion-provider
integration、具体 remote adapter、自动 background sweep/lease fencing、shared-service
worker、本机尚未运行的 PostgreSQL crash probe 与其余 crash matrix 仍未完成。
精确 legacy SQLite timestamp trigger 也会在 reopen 时原子修复。这些仍只是新增证据与
corruption repair，没有提升 atom；
正式与候选进度均保持 182/490（37.14%）。

F3 provider-effect 基础包含一个严格 provider-transition event、内容寻址 attempt/invocation/
receipt/reconciliation identity、`effect-queue` reducer version 3 与 authenticated
generic-ledger service。SQLite 会核验精确 append replay、receipt mismatch 拒绝、保守的
orphan fail-closed 行为、request-only 原子 claim、精确 owner-fence attestation、provider-
bound recovery、unknown-result reconciliation、已保留 retry timing、有界 retry/dead-letter、
receipt-backed generic compensation 与 commit 后 response loss；同一
存储中立路径在具备所需 executable 时还有 PostgreSQL integration test。配置后的显式
durable runtime 已选择 server-owned Semantic invocation；提供相应依赖时，可信 reconciliation、
owner-fence verification 与有界 retry/dead-letter 会核验精确保留 evidence。receipt 绑定完整
structured result，同 scope 的新 authorization 可以进行对账，且所有
已配置 transport 产生相同 provider-effect sequence。PostgreSQL provider hard-crash test 已
加入，但当前机器缺少 PostgreSQL executable，因而被跳过。completion-provider integration、
具体 remote-provider adapter、自动 background sweep/lease fencing、shared-service worker、
其余 crash matrix 与 remote exactly-once 仍未完成。计划没有可安全使用的显式 atom-ID
映射，因此没有完整 F3 effect atom 被提升；正式与候选进度仍为 182/490（37.14%），
`atom_ids=[]`。

有序 Trace event 协议现已新增一个注册 observation family、严格
source/time/Artifact/tool/permission/parent-subagent linkage，以及与 ledger 兼容、用于至多
100 条连续 event 原子批次的 partition identity/content digest。typed preflight、SQLite
续接与精确 append/replay 已核验，精确 SQLite/PostgreSQL receipt/page parity test 也已存在，
但本机没有 PostgreSQL executable。
Codex Hook/App Server ingestion、Trace reducer 与默认 Trace persistence cutover 仍未完成。
固定进度契约没有可审计的 F3 atom-ID→plan-line 映射，因此
不会为这些协议证据臆造 atom ID；正式与候选进度继续为 182/490（37.14%），
`atom_ids=[]`。

Git observation 协议现已新增八种已注册的 checkout、commit、ref、worktree-status、
diff、commit-relation、object-availability 与 shallow-state event。opt-in recorder
在保持冻结的既有 capture 返回类型不变的同时，把规范 Git/runner/algorithm version、
partition 与 checkout identity、仅 Artifact 的精确 diff reference，以及保守的 unknown
ancestry 绑定进 generic ledger。聚焦 SQLite append/replay 与既有 capture 兼容性已核验；
PostgreSQL parity coverage 已存在，但因本机缺少 PostgreSQL executable 而跳过。event
payload 排除 raw path、remote URL、diff 字节、stdout 与 stderr。自动 Git/diff capture、
checkout authority、force-push reconciliation、Codex Hook/App
Server ingestion 与默认 cutover 仍未完成。本增量没有可安全映射的完整固定计划 atom ID，
因此正式与候选进度仍为 182/490（37.14%），`atom_ids=[]`。

F3 Git graph reducer 现在会通过 sealed registry 消费全部八种 typed Git observation event，
并确定性重建 strict-scope commit node、checkout/ref history、pairwise ancestry assertion/
confidence summary、current missing-object state 与精确 last-observation provenance。它会把
矛盾 ancestry 保持为 `unknown/conflicted`，按 ledger global order 而不是 wall-clock order
处理，并在 version-1 event 无法证明时让 direct-parent edge、force-push claim、source/fix/
verification relationship 与 PR anchor 保持为空。默认 registry checkpoint 轮换、聚焦
determinism/contradiction/scope test 与 public operator resolution 已覆盖。active
applicability/PR-risk consumer、精确 evidence/PR join、自动 capture 与默认 cutover 仍未完成。
固定契约仍没有这个部分 F3 cluster 的安全 atom-ID 映射，因此正式与候选进度保持
182/490（37.14%），`atom_ids=[]`。
