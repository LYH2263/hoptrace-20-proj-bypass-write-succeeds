"""projection_attestation — 投影同源核证实体。

每条核证记录把一次「投影写入决策」落成可核对的只读证据，字段覆盖题目要求的
末 hop、映射码、投影值、是否旁路拒绝：

* ``kind='derived'``：正式时间线成功变更的同事务重算，核证随投影一起提交，
  因此「时间线变更与投影同成败」。
* ``kind='bypass_rejected'``：直接改投影入口被拒。该记录由独立连接写入，
  与被回滚的业务事务分离——业务写一律不留半成功，核证本身是只读审计证据。

只读接口见 ``GET /api/projection-attestations``。
"""

from __future__ import annotations

from datetime import datetime, timezone

KIND_DERIVED = "derived"
KIND_BYPASS_REJECTED = "bypass_rejected"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def insert(conn, record: dict) -> int:
    """插入一条核证记录，返回其 id。调用方自行管理事务。"""
    cur = conn.execute(
        """
        INSERT INTO projection_attestations(
          waybill_id, kind, bypass_rejected, last_hop_id, mapped_code,
          proj_value, timeline_digest, attempted_value, reason, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            record["waybill_id"],
            record["kind"],
            1 if record.get("bypass_rejected") else 0,
            record.get("last_hop_id"),
            record.get("mapped_code"),
            record.get("proj_value"),
            record.get("timeline_digest"),
            record.get("attempted_value"),
            record.get("reason"),
            record.get("created_at") or now_utc(),
        ),
    )
    return int(cur.lastrowid)


def list_for(conn, waybill_id: str | None = None, limit: int = 100) -> list[dict]:
    if waybill_id:
        rows = conn.execute(
            "SELECT * FROM projection_attestations WHERE waybill_id=? ORDER BY id DESC LIMIT ?",
            (waybill_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM projection_attestations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["bypass_rejected"] = bool(d["bypass_rejected"])
        out.append(d)
    return out
