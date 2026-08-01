# 事件类型注册表 v1

[English](event-registry-v1.md) | **简体中文**

`tbm.event-registry.v1` 是规范 `tbm.event.v1` 信封的存储中立 typed-consumption
边界。即使事件类型或版本未知，规范信封仍然可以原样追加和保存。reducer、projector
或其他类型化 consumer 必须通过已密封的注册表解析事件；没有匹配注册时必须显式失败。

## 注册与密封

每个 `EventPayloadRegistration` 把唯一的 `(event_type, event_version)` 绑定到事件
种类、带版本的 payload Schema 名称、严格的根对象 JSON Schema，以及经过域分隔的
Schema 哈希。根 payload Schema 必须拒绝额外属性。无依赖的受支持 Schema 子集覆盖
严格对象、数组、标量、enum、const、`oneOf`、正则表达式、长度/基数、唯一性和数值
边界。

重复 type/version 和重复 payload Schema 名称都会失败。注册表只在组装期间可变；
`seal()` 会冻结 registration/upcaster 拓扑。检查、类型化消费、兼容矩阵和 Schema
生成都要求注册表非空且已经密封。

版本 1 把 catalog 限制为 32 个 event type、每类 32 个 version、2,048 条 upcaster
edge 与 32,768 行 compatibility；发布的 catalog Schema 使用相同限制。

Schema keyword 的类型及其 object/array/string/number 上下文都会严格核验。property
name 和每次 upcaster 输出还会经过 canonical event payload 的容量限制与 forbidden
secret-metadata policy，因此 typed evolution 不能绕过基础信封的安全边界。

## 未知事件

`inspect()` 返回原始不可变 `CanonicalEvent`，状态为以下之一：

- `known`；
- `unknown_type`；
- `unknown_version`。

因此未知事件会被精确保留，可供导出、迁移或未来软件处理。`consume()` 绝不会把它们
当成空载荷或通用载荷，而是抛出稳定的 `TBM_EVENT_REGISTRY_UNKNOWN_EVENT`；错误对象
保留原始事件，供受控的 operator 路径处理。这是未来 reducer 必须遵守的未知事件行为。

## Upcaster 与兼容性

`EventPayloadUpcaster` 每次只前进一个版本，并绑定显式 upcaster ID 和生产者版本。
两个端点必须先注册并保持相同事件种类。跨多版本转换只能沿完整的相邻边链执行。每个
中间输出都会被复制、限制，并根据目标 payload Schema 重新验证。失败消息经过清理；
规范源事件永远不会被改写。

生成的兼容矩阵把每个已注册 source/target 组合标记为 `native`、`upcast` 或
`unsupported`。downcast 始终不支持。Upcaster 元数据进入内容寻址的注册表目录；
已部署代码的来源仍由发布和分发流程负责。

## 确定性产物

已密封的默认注册表当前发布规范事件示例使用的契约：`tbm.memory.proposed` version 1。
它确定性生成：

- `examples/event_type_registry_v1.example.json`——内容寻址目录；
- `schemas/event_type_registry_v1.schema.json`——目录结构预检 Schema；
- `schemas/event_payload_registry_v1.schema.json`——由 registrations 生成的类型化
  payload dispatch Schema。

规范资源与安装资源逐字节一致。新增生产事件类型时，必须在同一变更中提供严格 payload
Schema、兼容性决策、聚焦的非法载荷测试和重新生成的资源字节。

本注册表不追加事件、不授权调用者、不选择 ledger、不执行 reducer，也不会让未知事件
变得可安全消费。
