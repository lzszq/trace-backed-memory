# 授权 v3 契约

[English](authorization-v3.md) | **简体中文**

状态：已发布的准备性契约，并包含 opt-in 隔离 SQLite 与 PostgreSQL
authorization authority。它们尚未接入现行 snapshot-v2 Store、本地 Agent、MCP 适配器或
GateSession 仓储；这些路径仍保持其他文档所述的进程内或显式 opt-in 边界。

授权 v3 定义未来服务在读取任何租户或仓库数据之前所需的身份、仓库注册表、角色
绑定、请求与内容派生决策。它不会把适用性匹配变成授权。

## 信任边界

`principal_id`、`agent_client_id`、租户上下文和仓库引用必须来自经过认证、由
服务端持有的请求上下文。调用方提交的 JSON 字段不是身份证明。该契约不会保存
密码、bearer token、会话秘密或可重建凭据的材料。

`authorization_event_id`、`request_sha256` 与 `policy_sha256` 是确定性的内容
身份，用于发现不一致并建立精确关联；它们不是签名、MAC，也不能证明不可信生产
方的真实性。消费方必须针对精确信任策略和请求调用
`verify_authorization_decision(policy, request, decision)`，不得把孤立的决策
文档当作授权凭据。

允许决策是一次时点评估，不是长期 capability。每个受保护操作之前都应按当前策略
重新评估，或确认该精确信任策略仍是权威版本；不得重放旧决策绕过撤销或过期。

`SQLiteAuthorizationV3Repository` 与 `PostgresAuthorizationV3Repository`
在隔离的 `schemas/*-v3-authorization.sql` schema 中持久化 immutable policy bundle
与关联 decision。`authorize_and_record()` 先求值再存储；`append_decision()`
要求精确 policy、request 与 decision，并在单个原子追加前调用
`verify_authorization_decision()`。request identity 唯一，精确重放幂等，冲突
重评估会被拒绝；已存 descriptor 会重验，schema drift fail closed，嵌套调用方
使用 savepoint。PostgreSQL install/rollback 是独立、带版本门禁的原子资源；
rollback 会拒绝 catalog drift 与外部依赖。这些 repository 不认证输入上下文，
也尚未成为 active retrieval boundary。

## 注册表与绑定

`AuthorizationPolicyBundle` 包含：

- 以稳定 ID 标识的 principal，包括 issuer、哈希 subject、可选租户和
  active/disabled 状态；
- 以稳定 ID 标识的 agent client，包括明确的 client kind、可选租户和
  active/disabled 状态；
- 规范仓库、每仓库恰好一个仓库到租户绑定，以及显式的租户作用域别名；
- 唯一的角色绑定，把一个 principal 与一个 agent client 连接到 global、tenant
  或 repository 作用域、显式权限集合、状态和有效时间区间。

策略构造时校验所有跨记录引用。别名精确且区分大小写，不做 trim、模糊匹配、路径
规范化或 provider 猜测。`CanonicalRepository.legacy_aliases` 只属于迁移证据，
绝不作为授权别名；运营方必须把接受的别名连同明确租户和来源加入
`repository_aliases`。

绑定在 `valid_from` 时刻生效，在 `expires_at` 时刻失效；revoked 绑定永不匹配。
`platform:admin` 是显式的全局超级用户权限，会匹配所有权限，必须按此风险分配和
审计。

## 求值顺序

符合契约的服务必须先授权、后检索：

1. 从服务端认证上下文取得 principal、client 和目标；
2. 拒绝未知、disabled 或租户不一致的身份；
3. 精确解析规范仓库 ID 或已注册的租户别名；
4. 拒绝仓库与租户不一致；
5. 为精确 principal-client 对查找状态有效、处于时间窗口内且权限与作用域覆盖
   请求的绑定；
6. 产生内容派生的允许或拒绝决策，并保留请求与策略关联；
7. 只有允许后才可开始检索和适用性过滤。

授权作用域可携带现有的有界适用性属性，例如 branch、model family 或 task type。
求值器有意忽略这些属性：它们只能在后续缩小适用范围，永远不能授予访问权。

仓库权限要求同时提供租户与仓库目标；`tenant:audit_read` 只要求租户；全局策略与
平台审计权限禁止携带租户或仓库目标。

## 线协议资源与边界

规范资源：

- `schemas/authorization_policy_v3.schema.json`
- `schemas/authorization_decision_v3.schema.json`
- `examples/authorization_policy_v3.example.json`
- `examples/authorization_decision_v3.example.json`
- `schemas/sqlite-v3-authorization.sql`
- `schemas/postgres-v3-authorization.sql`
- `schemas/postgres-v3-authorization-rollback.sql`

JSON loader 拒绝重复键、非有限数字、无效 UTF-8、未知或缺失字段、超过 1 MiB、
超过 25,000 个节点或深度超过 32 的输入。每个注册表最多 10,000 项，每个绑定
最多 32 个唯一权限。Python 校验还强制跨记录身份、租户、别名、作用域、时间、
决策排序关联与内容派生 ID 不变量。

## 兼容性边界

该契约不提升 snapshot 版本 2、SQLite schema 版本 1 或现行 PostgreSQL schema
版本 2。授权 schema 是隔离、opt-in 的 schema-version-1 authority；它们不增加
远程服务、认证 provider、SDK transport 或运行时注入路径。现行适配器接入必须先
具备显式迁移、服务端认证上下文、负向授权测试和跨适配器一致性验证，之后才能激活。
