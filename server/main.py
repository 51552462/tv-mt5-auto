import os, sqlite3, json, time
from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel
from typing import Optional, List

DB_PATH = os.environ.get(DB_PATH, signals.db)
AUTH_TOKEN = os.environ.get(AUTH_TOKEN)  # TradingView → Render 보안토큰(선택)
AGENT_KEY  = os.environ.get(AGENT_KEY)   # Agent(윈도우) 인증용 필수 토큰

app = FastAPI(title=TV→Render→MT5 Hub)

def init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
    CREATE TABLE IF NOT EXISTS signals(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at REAL NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
    ))
    conn.commit(); conn.close()

init_db()

class PullReq(BaseModel)
    agent_key str
    max_batch int = 10

def insert_signal(payload dict)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(INSERT INTO signals(created_at, payload, status) VALUES(,,),
                (time.time(), json.dumps(payload, ensure_ascii=False), queued))
    conn.commit()
    cur.execute(SELECT last_insert_rowid())
    rid = cur.fetchone()[0]
    conn.close()
    return rid

def pull_signals(n=10)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(SELECT id, payload FROM signals WHERE status='queued' ORDER BY id ASC LIMIT , (n,))
    rows = cur.fetchall()
    ids = [r[0] for r in rows]
    if ids
        cur.execute(fUPDATE signals SET status='reserved' WHERE id IN ({','.join(''len(ids))}), ids)
        conn.commit()
    conn.close()
    return [{id rid, payload json.loads(pl)} for rid,pl in rows]

def ack_signals(ids List[int], status=done)
    if not ids return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(fUPDATE signals SET status= WHERE id IN ({','.join(''len(ids))}),
                [status, ids])
    conn.commit(); conn.close()

@app.post(webhook)
async def webhook(request Request, authorization Optional[str] = Header(None))
    if AUTH_TOKEN
        if authorization != fBearer {AUTH_TOKEN}
            raise HTTPException(401, Unauthorized)
    data = await request.json()
    rid = insert_signal(data)
    return {ok True, id rid}

@app.post(pull)
async def pull(req PullReq)
    if req.agent_key != AGENT_KEY
        raise HTTPException(401, Unauthorized agent)
    items = pull_signals(req.max_batch)
    return {ok True, items items}

class AckReq(BaseModel)
    agent_key str
    ids List[int]
    status str = done  # or failed

@app.post(ack)
async def ack(req AckReq)
    if req.agent_key != AGENT_KEY
        raise HTTPException(401, Unauthorized agent)
    ack_signals(req.ids, req.status)
    return {ok True, count len(req.ids)}
