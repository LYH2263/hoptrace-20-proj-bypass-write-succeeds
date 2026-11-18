"""状态投影服务。

落库形态（择一，本实现选 **运单行持有不可变 status_proj 列**）：
``waybills.status_proj`` 是唯一的对外可读状态，只能由 ``refresh`` 在
「正式时间线成功变更」的同一事务内整体替换，并同步追加：

1. 状态证明 ``status_proofs``（运单 / 末有效 hop / 映射码 / 投影值 / 时间线摘要 / 时刻）；
2. 投影同源核证 ``projection_attestations``（同上，外加 ``bypass_rejected`` 标志）。

派生路径（择一，本实现选 **时间线每次成功变更时同事务重算**）：
1. 正式线写入 hop（``timeline.append_primary``）成功；
2. 在同一事务内重算末有效 hop → 映射 → 整体替换 status_proj；
3. 同一事务内追加状态证明与核证记录。
任一步失败整体回滚，因此不存在「投影已变而正式线未变」或反过来的分叉。

直接改投影的入口（``set_direct`` / 裸 UPDATE status_proj）一律整笔失败：
``set_direct`` 不做任何业务写即抛 ``DirectStatusWriteError``，调用事务回滚；
拒绝事件另由独立连接落一条 ``bypass_rejected`` 核证（审计证据，不属于业务写）。
数据库触发器还会拒绝任何裸 ``UPDATE waybills.status_proj``。
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.db import connect
from app.modules import projection_attestation as attest
from app.modules import status_from_timeline as sft
from app.services import timeline as timeline_svc

PROJECTION_SOURCE = "timeline:last_effective_hop"


class DirectStatusWriteError(Exception):
    """试图绕过正式时间线直接改写对外状态。"""

    def __init__(self, waybill_id: str, attempted: str | None = None):
        self.waybill_id = waybill_id
        self.attempted = attempted
        super().__init__(
            "拒绝直接改写状态投影：对外状态只能由正式时间线末有效 hop 的 "
            f"event_code 映射得出（运单 {waybill_id}，尝试值 {attempted!r}）。"
            "请改为向正式时间线装载 hop，再由投影服务同事务重算。"
        )


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh(conn, waybill_id: str, *, write_proof: bool = True) -> dict:
    """在同一事务内重算并整体替换投影值，追加状态证明与同源核证。

    调用方负责事务边界（``BEGIN IMMEDIATE`` / ``commit`` / ``rollback``）。
    运单行必须已存在；``status_proj`` 以「写后读」方式确认落库值。
    """
    derived = sft.derive(conn, waybill_id)
    # 一次性令牌：数据库触发器只接受携带令牌且值等于末 hop 映射的更新，
    # 因此裸 UPDATE 一律失败，而本服务仍走同一条更新语句。
    conn.execute(
        "INSERT INTO projection_write_tokens(waybill_id, proj_value, created_at) VALUES (?,?,?)",
        (waybill_id, derived["status"], now_utc()),
    )
    cur = conn.execute(
        "UPDATE waybills SET status_proj=? WHERE id=?",
        (derived["status"], waybill_id),
    )
    if cur.rowcount != 1:
        raise LookupError(f"运单不存在，无法投影: {waybill_id}")

    persisted = sft.declared_status(conn, waybill_id)
    if persisted != derived["status"]:
        # 理论上不可达；宁可整笔失败也不留下「投影已变但读不回」的状态。
        raise RuntimeError(
            f"投影写后读不一致: {waybill_id} 期望 {derived['status']!r} 实际 {persisted!r}"
        )

    proof = {
        "waybill_id": waybill_id,
        "last_hop_id": derived["last_hop"]["id"] if derived["last_hop"] else None,
        "mapped_code": derived["mapped_code"],
        "proj_value": derived["status"],
        "timeline_digest": derived["timeline_digest"],
        "created_at": now_utc(),
    }
    if write_proof:
        sft.insert_proof(conn, proof)

    # 同源核证与投影、证明在同一事务内落库：时间线变更与投影同成败。
    attest.insert(
        conn,
        {
            "waybill_id": waybill_id,
            "kind": attest.KIND_DERIVED,
            "bypass_rejected": False,
            "last_hop_id": proof["last_hop_id"],
            "mapped_code": proof["mapped_code"],
            "proj_value": proof["proj_value"],
            "timeline_digest": proof["timeline_digest"],
            "attempted_value": None,
            "reason": None,
            "created_at": proof["created_at"],
        },
    )
    return proof


def apply_timeline_change(conn, waybill_id: str) -> dict | None:
    """正式线变更后的统一入口：重算投影 + 追加证明/核证（调用方持有事务）。"""
    return refresh(conn, waybill_id)


def load_hop(conn, hop: dict) -> dict:
    """向正式时间线装载一条 hop；正式线一旦变更，投影必须在同事务内随末有效 hop 重算。

    幂等重放（hop 已存在、正式线未变更）不重算、不追加证明/核证。
    """
    written = timeline_svc.append_primary(conn, hop)
    waybill_id = hop["waybill_id"]
    proof = None
    if written:
        # 硬约束：正式线成功变更与投影重算绑定在同一事务，任一步失败整体回滚。
        proof = apply_timeline_change(conn, waybill_id)
    return {
        "written": written,
        "proof": proof,
        "status": sft.declared_status(conn, waybill_id),
    }


def set_direct(conn, waybill_id: str, value: str | None) -> None:
    """直接改写对外状态投影：不做任何业务写，整笔拒绝。

    触发 ``DirectStatusWriteError`` 后由调用方回滚事务；拒绝事件的核证记录
    由 ``record_bypass_rejection`` 用独立连接写入，不随业务事务回滚。
    """
    raise DirectStatusWriteError(waybill_id, value)


def record_bypass_rejection(waybill_id: str, attempted: str | None) -> dict | None:
    """用独立连接落一条旁路拒绝核证（只读审计证据，与业务事务隔离）。

    即使被拒运单当前没有正式 hop，也会记录；运单不存在则只保留尝试值与原因。
    核证写入失败不应掩盖原始拒绝，故内部吞掉异常并返回 None。
    """
    record = {
        "waybill_id": waybill_id,
        "kind": attest.KIND_BYPASS_REJECTED,
        "bypass_rejected": True,
        "last_hop_id": None,
        "mapped_code": None,
        "proj_value": None,
        "timeline_digest": None,
        "attempted_value": attempted,
        "reason": (
            "DIRECT_STATUS_WRITE_REJECTED: 对外投影只能由正式有效轴末 hop 映射；"
            "请改为向正式时间线装载 hop。"
        ),
        "created_at": now_utc(),
    }
    audit_conn = connect()
    try:
        wb = audit_conn.execute(
            "SELECT id FROM waybills WHERE id=?", (waybill_id,)
        ).fetchone()
        if wb is not None:
            derived = sft.derive(audit_conn, waybill_id)
            record["last_hop_id"] = (
                derived["last_hop"]["id"] if derived["last_hop"] else None
            )
            record["mapped_code"] = derived["mapped_code"]
            record["proj_value"] = derived["status"]
            record["timeline_digest"] = derived["timeline_digest"]
        audit_conn.execute("BEGIN IMMEDIATE")
        new_id = attest.insert(audit_conn, record)
        audit_conn.commit()
        record["id"] = new_id
        return record
    except Exception:
        audit_conn.rollback()
        return None
    finally:
        audit_conn.close()
