# GateSession recovery worker

[English](gate-recovery-worker-v3.md) | **简体中文**

`GateSessionRecoveryWorker` 对 SQLite 或 PostgreSQL `list_due()` 返回的未锁定
candidate snapshot 执行一次有界 recovery pass。它绝不把 discovery 当作锁或
durable claim。

worker 会在任何 mutation 前验证完整有界 page，然后每个 candidate 只处理一次：

- `PREPARED` 与 `AWAITING_DECISION` 尝试以精确 version transition 到
  `EXPIRED`；只有 session expiry（而不只是 lease）到达后才会成功；
- 只有 lease 到期且 head 未变化时返回 `recovery_required`；
- `DECIDED`、`FINALIZED` 与 `EXECUTING` 返回 `recovery_required`，因为当前
  state graph 没有合法的 due-worker terminal transition；
- current revision 更新时返回 `superseded`，不会盲目重试 stale version。

每个成功 expiry receipt 都必须保留全部 immutable session field，并被精确读回。
current-state read 必须保留原 session identity，且不能退回更旧 version。discovery、
read、receipt 与 limit failure 都使用有界稳定错误。

一次 pass 是一组独立 CAS operation，不是 batch transaction。若基础设施在早期
candidate 已提交后失败，可能出现 partial progress；下一次有界 pass 会重新发现
current head。独立 operator 或 recovery service 必须处理 `recovery_required`
结果，且不得重建进程内 Store token。
