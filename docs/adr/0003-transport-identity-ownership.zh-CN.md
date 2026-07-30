# ADR-0003：transport identity 归属

**状态：** 已接受
**日期：** 2026-07-30
**English:** [0003-transport-identity-ownership.md](0003-transport-identity-ownership.md)

## 背景

Durable lifecycle 需要已认证 caller、provider、evaluator、repository、tenant、
client 与 environment identity。若 operation JSON 可以提供其中任何一个，
不可信调用方就能选择 authorization scope。另一方面，transport-neutral durable
wire dispatcher 自身不能认证 peer。

## 决策

- 在 route/tool 选择和 request body 解析前完成 authentication。
- 可信 transport adapter 在 operation JSON 外派生不可变 service context；严格
  schema 拒绝未知 identity 字段。
- 对精确 operation、repository、tenant、environment、principal 与 client 执行
  authorization，并持久读回核验。
- Provider/evaluator credential 不得进入 request JSON、response、exception 或 log。
- 本地 STDIO 可以使用可信启动配置，但不得宣称 peer authentication 或共享
  multi-tenant isolation。
- Loopback HTTP 可以使用进程 bearer secret；非 loopback HTTP 必须使用 TLS 和可信
  authenticator。生产 OIDC/mTLS 可由 gateway/adapter 提供，不改变 Gate policy。
- Transport 不得实现 retrieval、Gate、rendering、replay 或 publication policy。

## 影响

每个 durable transport 发布前都必须覆盖 identity injection、重复 header、credential
脱敏、revocation/recheck 和 authorization-before-retrieval 测试。

## 退出证据

所有受支持 transport 派生相同 authenticated context，拒绝调用方选择 identity，并
通过 tenant/repository/environment 以及 provider/evaluator 负向隔离测试。
