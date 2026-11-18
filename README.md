# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 状态投影：只能由正式时间线派生

对外可读状态只有一个落库位置：`waybills.status_proj`（运单行不可变投影快照列，
随时间线整体替换）。它由投影服务在「正式 hop 成功写入」的**同一事务**内整体替换，
并同步追加两类证据：

- 状态证明 `status_proofs`（运单 / 末有效 hop / 映射码 / 投影值 / 时间线摘要 / 证明时刻）；
- 投影同源核证 `projection_attestations`（同上，外加 `kind` 与 `bypass_rejected`
  是否旁路拒绝标志），只读接口 `GET /api/projection-attestations`，
  支持 `?waybill_id=` 过滤，无任何改写入口。

- 映射唯一来源：`app.modules.status_from_timeline.EVENT_STATUS`。
- 直接改投影入口 `POST /api/waybills/{id}/status` 一律 `409`
  （错误码 `DIRECT_STATUS_WRITE_REJECTED`，响应可区分）并回滚，
  响应写明原因与正确做法；数据库触发器还会拒绝任何裸
  `UPDATE waybills.status_proj`。拒绝事件由独立连接落一条
  `bypass_rejected` 核证，业务事务本身零写入、不留半成功。
- 只读证明接口 `GET /api/waybills/{id}/status-proof`（含末 hop 与投影映射对照、
  最近核证与累计旁路拒绝数），同源清单 `GET /api/status-check`，
  总览 `/api/overview` 的一致数直接取自检查器。

硬约束（由测试与触发器共同保证）：

1. 禁止直接改投影成功落库——服务层拒绝 + DB 触发器双保险；
2. 禁止末 hop 已变、投影未变仍标装载成功——`load_hop` 中正式线一旦变更，
   投影/证明/核证必须在同一事务内重算，任一步失败整笔回滚；
3. 禁止投影已变、正式轴未变——投影值只能由当前末有效 hop 映射，
   触发器校验新值等于映射值。

验收两条路径：

```bash
cd backend
python -m pytest -q tests
```

`test_bypass_write_fails_and_leaves_no_half_success`（旁路写整笔失败、
响应可区分、拒绝留核证）与
`test_projection_follows_last_hop_with_derived_attestation`
（装载后投影随末 hop 变，derived 核证给出末 hop 与投影映射对照）；
基线两条 `test_direct_status_write_fails_whole` /
`test_projection_follows_last_effective_hop_after_load` 同样保留并通过。
