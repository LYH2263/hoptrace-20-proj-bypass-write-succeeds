import json

from app.db import FIXTURES_DIR, connect
from app.modules import status_from_timeline as sft
from app.services import projection

SCHEMA = """
CREATE TABLE IF NOT EXISTS waybills (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status_proj TEXT NOT NULL DEFAULT '未知',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hops (
  id TEXT PRIMARY KEY,
  waybill_id TEXT NOT NULL REFERENCES waybills(id),
  event_ts TEXT NOT NULL,
  location_code TEXT NOT NULL,
  event_code TEXT NOT NULL,
  seq INTEGER,
  channel TEXT NOT NULL DEFAULT 'primary',
  retracted INTEGER NOT NULL DEFAULT 0,
  UNIQUE(waybill_id, event_ts, location_code, event_code)
);

CREATE TABLE IF NOT EXISTS quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  member_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_batches (
  id TEXT PRIMARY KEY,
  cutoff_ts TEXT,
  created_at TEXT NOT NULL,
  report_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS status_proofs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT NOT NULL,
  timeline_digest TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projection_attestations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  bypass_rejected INTEGER NOT NULL DEFAULT 0,
  last_hop_id TEXT,
  mapped_code TEXT,
  proj_value TEXT,
  timeline_digest TEXT,
  attempted_value TEXT,
  reason TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projection_attestations_wb
  ON projection_attestations(waybill_id, id);
"""


def _status_guard_sql() -> str:
    """把映射镜像到 SQLite 触发器，作为数据库层兜底。

    Python 映射仍是唯一事实来源。任何 `UPDATE waybills.status_proj`：

    1. 必须携带投影服务在同一事务内写入的一次性令牌
       （``projection_write_tokens``），否则视为「直接改投影」而整笔失败；
    2. 令牌命中的新值还必须等于正式时间线末有效 hop 的映射值，否则失败。

    令牌由触发器消费后立即删除，裸 UPDATE 无法伪造或复用。
    """
    branches = "\n".join(
        f"      WHEN {_sql_literal(code)} THEN {_sql_literal(status)}"
        for code, status in sft.EVENT_STATUS.items()
    )
    unknown = _sql_literal(sft.UNKNOWN_STATUS)
    return f"""
CREATE TABLE IF NOT EXISTS projection_write_tokens (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  waybill_id TEXT NOT NULL,
  proj_value TEXT NOT NULL,
  created_at TEXT NOT NULL
);

DROP TRIGGER IF EXISTS waybills_status_proj_guard;
CREATE TRIGGER waybills_status_proj_guard
BEFORE UPDATE OF status_proj ON waybills
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NOT EXISTS (
    SELECT 1 FROM projection_write_tokens
    WHERE waybill_id = NEW.id AND proj_value = NEW.status_proj
  ) THEN RAISE(ABORT, '状态投影必须由正式时间线末有效 hop 派生，禁止直接改写') END;

  SELECT CASE WHEN NEW.status_proj <> (
    SELECT COALESCE(
      CASE (
        SELECT h.event_code FROM hops h
        WHERE h.waybill_id = NEW.id AND h.channel='primary' AND h.retracted=0
        ORDER BY h.event_ts DESC, h.id DESC LIMIT 1
      )
{branches}
      END,
      {unknown}
    )
  ) THEN RAISE(ABORT, '状态投影必须由正式时间线末有效 hop 派生，禁止直接改写') END;

  DELETE FROM projection_write_tokens
  WHERE waybill_id = NEW.id AND proj_value = NEW.status_proj;
END;

DROP TRIGGER IF EXISTS waybills_status_proj_insert_guard;
CREATE TRIGGER waybills_status_proj_insert_guard
BEFORE INSERT ON waybills
FOR EACH ROW
BEGIN
  SELECT CASE WHEN NEW.status_proj <> (
    SELECT COALESCE(
      CASE (
        SELECT h.event_code FROM hops h
        WHERE h.waybill_id = NEW.id AND h.channel='primary' AND h.retracted=0
        ORDER BY h.event_ts DESC, h.id DESC LIMIT 1
      )
{branches}
      END,
      {unknown}
    )
  ) THEN RAISE(ABORT, '新建运单不得自带状态投影；装载 hop 后由投影服务派生') END;
END;
"""


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _migrate(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(status_proofs)")}
    if "timeline_digest" not in cols:
        conn.execute("ALTER TABLE status_proofs ADD COLUMN timeline_digest TEXT")

    # 旧库补建投影同源核证表（基线所无的新实体）。
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "projection_attestations" not in tables:
        conn.execute(
            """
            CREATE TABLE projection_attestations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              waybill_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              bypass_rejected INTEGER NOT NULL DEFAULT 0,
              last_hop_id TEXT,
              mapped_code TEXT,
              proj_value TEXT,
              timeline_digest TEXT,
              attempted_value TEXT,
              reason TEXT,
              created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_projection_attestations_wb "
            "ON projection_attestations(waybill_id, id)"
        )

    # 表为空且已有运单 => 旧库升级，按当前正式时间线回填 derived 核证。
    att_count = conn.execute(
        "SELECT COUNT(*) AS c FROM projection_attestations"
    ).fetchone()["c"]
    wb_count = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
    if att_count == 0 and wb_count > 0:
        _backfill_attestations(conn)


def _backfill_attestations(conn) -> None:
    """旧库升级：为缺少 derived 核证的运单按当前正式时间线补一条核证。

    只补审计证据，不触碰 status_proj（其值已由既有触发器约束为映射值）。
    """
    from app.modules import projection_attestation as attest
    from app.modules import status_from_timeline as sft

    rows = conn.execute("SELECT id FROM waybills ORDER BY id").fetchall()
    for row in rows:
        wid = row["id"]
        exists = conn.execute(
            "SELECT 1 FROM projection_attestations WHERE waybill_id=? AND kind='derived' LIMIT 1",
            (wid,),
        ).fetchone()
        if exists:
            continue
        derived = sft.derive(conn, wid)
        attest.insert(
            conn,
            {
                "waybill_id": wid,
                "kind": attest.KIND_DERIVED,
                "bypass_rejected": False,
                "last_hop_id": derived["last_hop"]["id"] if derived["last_hop"] else None,
                "mapped_code": derived["mapped_code"],
                "proj_value": derived["status"],
                "timeline_digest": derived["timeline_digest"],
                "attempted_value": None,
                "reason": "backfill: 旧库升级补建同源核证",
            },
        )


def init_db():
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.executescript(_status_guard_sql())
        _migrate(conn)
        n = conn.execute("SELECT COUNT(*) AS c FROM waybills").fetchone()["c"]
        if n == 0:
            _seed_fixtures(conn)
        conn.commit()
    finally:
        conn.close()


def _seed_fixtures(conn):
    """本地夹具装载：运单一律以正式时间线推导投影，忽略夹具里的 status_proj。

    夹具中的 ``status_proj`` 只作为展示性历史值，不参与投影；
    这样基座启动时就不会带着「投影与末 hop 不一致」的既成分叉。
    """
    fixture = FIXTURES_DIR / "seed_hops.json"
    if not fixture.exists():
        return
    data = json.loads(fixture.read_text(encoding="utf-8"))
    for wb in data.get("waybills", []):
        conn.execute(
            "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
            (wb["id"], wb["title"], sft.UNKNOWN_STATUS, wb["created_at"]),
        )
    for hop in data.get("hops", []):
        conn.execute(
            "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) VALUES (?,?,?,?,?,?,?)",
            (
                hop["id"],
                hop["waybill_id"],
                hop["event_ts"],
                hop["location_code"],
                hop["event_code"],
                hop.get("seq"),
                hop.get("channel", "primary"),
            ),
        )
    for wb in data.get("waybills", []):
        projection.refresh(conn, wb["id"])


def map_status(event_code: str) -> str:
    """兼容旧导入路径，映射仍只有一份定义。"""
    return sft.map_status(event_code)
