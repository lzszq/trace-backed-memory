# ADR-0004：规范 resource manifest

**状态：** 已接受
**日期：** 2026-07-30
**English:** [0004-canonical-resource-manifest.md](0004-canonical-resource-manifest.md)

## 背景

规范 schema、example、SQL 与 policy 文件会镜像到安装包资源。严格 allowlist 是
安全和分发保证，但手工同步 `pyproject.toml`、`_RESOURCE_SPECS`、文档与 fixture
计数容易出错。

## 决策

- 引入一个规范、确定性的 resource manifest。
- manifest 记录公开资源名、规范源路径、安装路径、media type 与 SHA-256。
- 仓库 generator/checker 生成或核验 package-data、`_RESOURCE_SPECS`、文档资源
  索引与 distribution expectation。
- 规范文件仍在安装资源树外创作；安装副本保持逐字节一致。
- 生成必须显式、离线。runtime import、测试和验证均不得读取网络资源。
- 生成输出或复制字节漂移时 CI 必须失败。

## 影响

增加公开资源只需更新一处 manifest 与规范字节，checker 会指出所有受影响的生成
表示。manifest 不会放宽 allowlist。

## 退出证据

Wheel、sdist、editable install、公开 resource API 与 clean regeneration 必须报告
相同的有序资源集合和精确 digest。
