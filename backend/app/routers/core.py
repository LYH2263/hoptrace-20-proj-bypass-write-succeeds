from fastapi import APIRouter, Body, HTTPException

from app.db import connect
from app.modules import status_from_timeline as sft
from app.services import ingest as ingest_svc
from app.services import inspect as inspect_svc
from app.services import projection
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        # 总览计数与检查器同源：不旁路统计投影一致性。
        return inspect_svc.overview(conn)
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, title, status_proj, created_at FROM waybills ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        mat = timeline_svc.materialize(conn, waybill_id)
        check = inspect_svc.check_waybill(conn, waybill_id)
        return {
            "waybill": dict(wb),
            "hops": mat["hops"],
            "last_effective_hop": mat["last_effective_hop"],
            "timeline_digest": mat["timeline_digest"],
            "proof": sft.latest_proof(conn, waybill_id),
            "check": check,
        }
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/status-proof")
def waybill_status_proof(waybill_id: str):
    """按运单查询的只读状态证明。

    返回运单、末有效 hop、映射码、投影值、证明时刻，并同时给出
    「由当前正式时间线重新推导」的对照值与同源判定。
    """
    conn = connect()
    try:
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        if not wb:
            raise HTTPException(404, "运单不存在")
        mat = timeline_svc.materialize(conn, waybill_id)
        derived = sft.derive(conn, waybill_id)
        proof = sft.latest_proof(conn, waybill_id)
        latest_attestation = conn.execute(
            "SELECT * FROM projection_attestations WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        bypass_rejections = conn.execute(
            "SELECT COUNT(*) AS c FROM projection_attestations "
            "WHERE waybill_id=? AND bypass_rejected=1",
            (waybill_id,),
        ).fetchone()["c"]
        return {
            "waybill_id": waybill_id,
            "status_proj": wb["status_proj"],
            "last_hop": mat["last_effective_hop"],
            "mapped_code": derived["mapped_code"],
            "proj_value": derived["status"],
            "proof_at": proof["created_at"] if proof else None,
            "proof": proof,
            "latest_attestation": dict(latest_attestation) if latest_attestation else None,
            "bypass_rejections": bypass_rejections,
            "timeline_digest": mat["timeline_digest"],
            "同源": sft.proof_matches(proof, derived)
            and proof["proj_value"] == wb["status_proj"],
            "source": projection.PROJECTION_SOURCE,
        }
    finally:
        conn.close()


@router.post("/waybills/{waybill_id}/hops")
def load_hop(waybill_id: str, body: dict = Body(...)):
    """向正式时间线装载一条 hop；投影在同事务内随末有效 hop 更新。"""
    hop = {**body, "waybill_id": waybill_id}
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM waybills WHERE id=?", (waybill_id,)).fetchone():
            conn.execute(
                "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                (waybill_id, body.get("waybill_title") or waybill_id, "未知", projection.now_utc()),
            )
        outcome = projection.load_hop(conn, hop)
        conn.commit()
        return {"ok": True, **outcome}
    except (ValueError, LookupError) as e:
        conn.rollback()
        raise HTTPException(422, str(e))
    except Exception as e:
        conn.rollback()
        raise HTTPException(500, str(e))
    finally:
        conn.close()


@router.post("/waybills/{waybill_id}/status")
def write_status_direct(waybill_id: str, body: dict = Body(...)):
    """直接改写对外状态的入口：整笔失败并写明原因，绝不半成功。

    ``set_direct`` 不执行任何业务写即抛 ``DirectStatusWriteError``；
    事务回滚后，用独立连接落一条 ``bypass_rejected`` 核证（审计证据）。
    """
    attempted = body.get("status_proj") or body.get("status")
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        projection.set_direct(conn, waybill_id, attempted)  # 必抛，下面不可达
        conn.commit()
        raise HTTPException(500, "内部错误：直接改写未被拒绝")
    except projection.DirectStatusWriteError as e:
        conn.rollback()
        # 业务事务已回滚（无任何半成功）；核证走独立连接，不污染业务写。
        attestation = projection.record_bypass_rejection(waybill_id, attempted)
        raise HTTPException(
            409,
            {
                "ok": False,
                "error": "DIRECT_STATUS_WRITE_REJECTED",
                "waybill_id": waybill_id,
                "attempted": attempted,
                "reason": str(e),
                "how_to_fix": "请向正式时间线装载 hop，投影会随末有效 hop 自动重算。",
                "rolled_back": True,
                "attestation_id": attestation["id"] if attestation else None,
            },
        )
    finally:
        conn.close()


@router.get("/projection-attestations")
def projection_attestations(waybill_id: str | None = None, limit: int = 100):
    """只读：投影同源核证实体清单。

    每条记录给出末有效 hop、映射码、投影值与 ``bypass_rejected``（是否旁路拒绝），
    可按运单过滤。该实体只由投影服务写入，接口不提供任何改写入口。
    """
    from app.modules import projection_attestation as attest

    conn = connect()
    try:
        rows = attest.list_for(conn, waybill_id, min(max(limit, 1), 500))
        return {"source": projection.PROJECTION_SOURCE, "items": rows}
    finally:
        conn.close()


@router.get("/status-check")
def status_check():
    """检查器：逐运单比对投影、末有效 hop 与状态证明。"""
    conn = connect()
    try:
        return inspect_svc.check_all(conn)
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

