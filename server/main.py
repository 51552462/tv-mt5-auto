# /server/main.py
import os
import sqlite3
import json
import time
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel

# ===================== 환경변수 =====================
DB_PATH    = os.environ.get("DB_PATH", "/tmp/signals.db")
AUTH_TOKEN = os.environ.get("AUTH_TOKEN")     # TradingView -> Render 인증(Bearer), 선택
AGENT_KEY  = os.environ.get("AGENT_KEY")      # Agent(Windows) 인증 필수 토큰
# ===================================================

app = FastAPI(title="TV→Render→MT5 Hub")

# ----------------- DB 유틸 -----------------
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL    NOT NULL,
            payload    TEXT    NOT NULL,
            status     TEXT    NOT NULL DEFAULT 'queued'
        )
        """
    )
    conn.commit()
    conn.close()

init_db()

def insert_signal(payload: Dict[str, Any]) -> int:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO signals (created_at, payload, status) VALUES (?, ?, 'queued')",
        (time.time(), json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    cur.execute("SELECT last_insert_rowid()")
    rid = int(cur.fetchone()[0])
    conn.close()
    return rid

def pull_signals(limit: int = 10) -> List[Dict[str, Any]]:
    conn = _db()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, payload FROM signals WHERE status='queued' ORDER BY id ASC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    ids = [int(r["id"]) for r in rows]
    if ids:
        qmarks = ",".join(["?"] * len(ids))
        cur.execute(f"UPDATE signals SET status='reserved' WHERE id IN ({qmarks})", ids)
        conn.commit()
    conn.close()
    return [{"id": int(r["id"]), "payload": json.loads(r["payload"])} for r in rows]

def ack_signals(ids: List[int], status: str = "done") -> None:
    if not ids:
        return
    conn = _db()
    cur = conn.cursor()
    qmarks = ",".join(["?"] * len(ids))
    cur.execute(f"UPDATE signals SET status=? WHERE id IN ({qmarks})", [status, *ids])
    conn.commit()
    conn.close()

def count_by_status() -> Dict[str, int]:
    conn = _db()
    cur = conn.cursor()
    cur.execute("SELECT status, COUNT(*) c FROM signals GROUP BY status")
    rows = cur.fetchall()
    conn.close()
    return {r["status"]: r["c"] for r in rows}

# ----------------- 스키마 -----------------
class PullReq(BaseModel):
    agent_key: str
    max_batch: int = 10

class AckReq(BaseModel):
    agent_key: str
    ids: List[int]
    status: str = "done"   # or "failed"

# ----------------- 라우트 -----------------
@app.get("/health")
def health():
    return {"ok": True, "db": DB_PATH, "stats": count_by_status()}

@app.post("/webhook")
async def webhook(request: Request, authorization: Optional[str] = Header(None)):
    """
    TradingView가 호출.
    - 인증을 쓰고 싶으면 Render 환경변수에 AUTH_TOKEN을 넣고,
      헤더에 Authorization: Bearer <AUTH_TOKEN> 를 보내면 됨.
    - (추가) 쿼리파라미터 ?auth=<AUTH_TOKEN> 또는 ?token=<AUTH_TOKEN> 도 허용.
    - 바디(JSON)는 그대로 큐에 저장되어 에이전트가 /pull로 가져가게 됨.
    """
    if AUTH_TOKEN:
        expected = f"Bearer {AUTH_TOKEN}"
        # 헤더 또는 쿼리파라미터 중 하나만 맞아도 통과
        qs = dict(request.query_params)
        header_ok = (authorization == expected)
        query_ok  = (qs.get("auth") == AUTH_TOKEN) or (qs.get("token") == AUTH_TOKEN)
        if not (header_ok or query_ok):
            raise HTTPException(401, "Unauthorized")

    try:
        data = await request.json()
    except Exception:
        # 비JSON이면 raw body 그대로 저장
        data = {"raw": await request.body()}

    rid = insert_signal(data)
    return {"ok": True, "id": rid}

@app.post("/pull")
async def pull(req: PullReq):
    """
    Windows 에이전트가 작업을 가져가는 엔드포인트.
    """
    if not AGENT_KEY or req.agent_key != AGENT_KEY:
        raise HTTPException(401, "Unauthorized agent")
    items = pull_signals(max(1, min(req.max_batch, 100)))
    return {"ok": True, "items": items}

@app.post("/ack")
async def ack(req: AckReq):
    """
    Windows 에이전트가 처리 결과를 보고하는 엔드포인트.
    status: "done" 또는 "failed"
    """
    if not AGENT_KEY or req.agent_key != AGENT_KEY:
        raise HTTPException(401, "Unauthorized agent")
    ack_signals(req.ids, req.status)
    return {"ok": True, "count": len(req.ids)}
