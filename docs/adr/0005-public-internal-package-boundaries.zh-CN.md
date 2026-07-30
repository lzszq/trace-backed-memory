# ADR-0005：公开与内部 package 边界

**状态：** 已接受
**日期：** 2026-07-30
**English:** [0005-public-internal-package-boundaries.md](0005-public-internal-package-boundaries.md)

## 背景

源码包包含大量顶层 v3 contract、service 与 SQLite/PostgreSQL authority。若从
package root 重新导出所有实现，就会让内部组合看起来像稳定 API，隐藏 dependency
direction，并增加 import cycle 和 optional dependency 风险。

## 决策

- 为兼容性保留已有且文档化的 package-root export。
- 新内部代码必须从所属模块 import，不能经过 package root。
- 后续迁移围绕 compatibility、contracts、services、authorities、transports、
  SDK、migrations、resources 组织。
- 仅通过明确的 transport contract、client type 和 runtime factory 暴露精简的
  durable public surface；repository 与 service graph 细节保持内部，除非独立记录。
- 不能因为 import package root 就把 optional transport dependency 变成必需依赖。
- 物理移动 package 前先增加 dependency-direction 和 import-smoke 测试。

## 影响

Package 重组采用增量 compatibility re-export，不做一次性迁移。源码文件存在不代表
它属于 public API。

## 退出证据

受支持 root import 保持稳定，内部模块不经 root import，optional extra 仍可选，
dependency-direction 测试会拒绝 cycle 和 transport→policy 反向依赖。
