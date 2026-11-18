"""总览与检查器：投影是否与正式时间线同源。

检查器逐运单比对：
* 运单行 ``status_proj``（对外可读值）
* 由正式时间线末有效 hop 重新映射得到的值
* 最近一条状态证明（末 hop / 映射码 / 投影值 / 时间线摘要）
* 最近一条投影同源核证（含 ``bypass_rejected`` 旁路拒绝标志）

三者任一不一致即判定分叉，检查器不会修正数据，只报告。
"""

from __future__ import annotations

from app.modules import status_from_timeline as sft
from app.services import timeline as timeline_svc


def _waybill_ids(conn) -> list[str]:
    rows = conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
    return [r["id"] for r in rows]


def _latest_attestation(conn, waybill_id: str, *, kind: str | None = None) -> dict | None:
    sql = (
        "SELECT * FROM projection_attestations WHERE waybill_id=? "
        "ORDER BY id DESC LIMIT 1"
    )
    params: tuple = (waybill_id,)
    if kind is not None:
        sql = (
            "SELECT * FROM projection_attestations WHERE waybill_id=? AND kind=? "
            "ORDER BY id DESC LIMIT 1"
        )
        params = (waybill_id, kind)
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["bypass_rejected"] = bool(data["bypass_rejected"])
    return data


def check_waybill(conn, waybill_id: str) -> dict:
    mat = timeline_svc.materialize(conn, waybill_id)
    last = mat["last_effective_hop"]
    expected = sft.map_status(last["event_code"] if last else None)
    actual = sft.declared_status(conn, waybill_id)
    proof = sft.latest_proof(conn, waybill_id)
    latest_attestation = _latest_attestation(conn, waybill_id)
    # 同源一致性只看最近一条 derived 核证；bypass_rejected 核证不改变投影。
    derived_attestation = _latest_attestation(
        conn, waybill_id, kind="derived"
    )
    proof_ok = sft.proof_matches(proof, {
        "status": expected,
        "last_hop": last,
        "mapped_code": last["event_code"] if last else None,
        "timeline_digest": mat["timeline_digest"],
    })

    divergences: list[str] = []
    if expected != actual:
        divergences.append("投影值与末有效 hop 映射不一致")
    if not proof_ok:
        if proof is None:
            divergences.append("缺少状态证明")
        else:
            divergences.append("状态证明与当前正式时间线不一致")
    if derived_attestation is None:
        divergences.append("缺少投影同源核证")
    elif (
        derived_attestation["proj_value"] != expected
        or (derived_attestation["last_hop_id"] or None)
        != (last["id"] if last else None)
        or (derived_attestation["mapped_code"] or None)
        != (last["event_code"] if last else None)
        or (derived_attestation["timeline_digest"] or None)
        != mat["timeline_digest"]
    ):
        divergences.append("投影同源核证与当前正式时间线不一致")

    return {
        "waybill_id": waybill_id,
        "status_proj": actual,
        "expected_status": expected,
        "last_hop_id": last["id"] if last else None,
        "mapped_code": last["event_code"] if last else None,
        "timeline_digest": mat["timeline_digest"],
        "hop_count": mat["hop_count"],
        "proof_id": proof["id"] if proof else None,
        "proof_created_at": proof["created_at"] if proof else None,
        "latest_attestation_id": latest_attestation["id"] if latest_attestation else None,
        "latest_attestation_kind": latest_attestation["kind"] if latest_attestation else None,
        "bypass_rejected": bool(
            latest_attestation and latest_attestation["bypass_rejected"]
        ),
        "同源": not divergences,
        "divergences": divergences,
    }


def check_all(conn) -> dict:
    items = [check_waybill(conn, wid) for wid in _waybill_ids(conn)]
    diverged = [i for i in items if not i["同源"]]
    return {
        "checked": len(items),
        "aligned": len(items) - len(diverged),
        "diverged": len(diverged),
        "items": items,
    }


def overview(conn) -> dict:
    """总览计数与检查器同源：投影一致数 / 分叉数直接取自检查结果。"""
    check = check_all(conn)
    return {
        "waybills": conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"],
        "hops_primary": conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
        ).fetchone()["c"],
        "hops_sidecar": conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
        ).fetchone()["c"],
        "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        "status_proofs": conn.execute(
            "SELECT COUNT(*) AS c FROM status_proofs"
        ).fetchone()["c"],
        "projection_attestations": conn.execute(
            "SELECT COUNT(*) AS c FROM projection_attestations"
        ).fetchone()["c"],
        "bypass_rejections": conn.execute(
            "SELECT COUNT(*) AS c FROM projection_attestations WHERE bypass_rejected=1"
        ).fetchone()["c"],
        "projection_aligned": check["aligned"],
        "projection_diverged": check["diverged"],
        "projection_source": "timeline:last_effective_hop",
    }
