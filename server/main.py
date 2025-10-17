# /server/main.py
import time
from collections import deque
from typing import Optional, Deque, Dict, Any
from math import isclose
import os

from fastapi import FastAPI, Request, Header, HTTPException
from pydantic import BaseModel

# ===================== 사용자 설정 =====================
# 트뷰 → 이 서버: 진입/분할/청산 판별 후 "작업 큐"에 적재
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.6"))  # 고정 진입 수량
BREAKEVEN_EPS = float(os.environ.get("BREAKEVEN_EPS", "0.0003"))   # 본절 데드존
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "set-me")              # 에이전트 인증 토큰

# TV 심볼 → (서버가 추천하는) MT5 심볼 후보
SYMBOL_ALIASES = {
    "NQ1!": ["US100", "NAS100", "USTEC", "US100.cash", "NAS100.cash", "NAS100m", "USTECH"]
}
# =======================================================

app = FastAPI(title="TV→MT5 Bridge (Render)")

# ------------ 내부 큐 ------------
TASKS: Deque[Dict[str, Any]] = deque()
TASK_AUTO_ID = 0
PENDING: Dict[int, Dict[str, Any]] = {}  # id -> task

def next_task_id() -> int:
    global TASK_AUTO_ID
    TASK_AUTO_ID += 1
    return TASK_AUTO_ID

def enqueue_task(task: Dict[str, Any]) -> Dict[str, Any]:
    task["id"] = next_task_id()
    task["created_at"] = time.time()
    TASKS.append(task)
    PENDING[task["id"]] = task
    return task

# ------------ 유틸 ------------
def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)

def find_mt5_symbol(tv_symbol: str) -> Optional[str]:
    for tv, candidates in SYMBOL_ALIASES.items():
        if tv_symbol.upper() == tv.upper():
            return candidates[0]
    return tv_symbol  # 모르면 그대로 넘기고, 에이전트에서 최종 매핑

def signed_amount(abs_amount: float, market_position: str) -> float:
    if market_position == "long":
        return +abs_amount
    if market_position == "short":
        return -abs_amount
    return 0.0

def same_sign(a: float, b: float) -> bool:
    return (a > 0 and b > 0) or (a < 0 and b < 0)

# ------------ 스키마 ------------
class TVPayload(BaseModel):
    symbol: str
    action: str                 # "buy" | "sell" (체결 방향)
    contracts: float
    pos_after: float            # 항상 양수(절대값)
    order_price: float | None = None
    market_position: str        # "long" | "short" | "flat"
    time: str | None = None

class TaskAck(BaseModel):
    id: int
    ok: bool
    detail: str | None = None

# 중복 필터
_last_key = None
_last_ts = 0.0

# ------------ 엔드포인트 ------------
@app.get("/health")
def health():
    return {"ok": True, "queue": len(TASKS), "pending": len(PENDING)}

@app.post("/webhook")
async def webhook(req: Request):
    """
    TradingView가 호출하는 엔드포인트.
    여기서 진입/분할/청산을 판별해 작업 큐에 넣습니다.
    """
    global _last_key, _last_ts
    data = await req.json()
    p = TVPayload(**data)

    mt5_symbol = find_mt5_symbol(p.symbol) or p.symbol
    dir_ = +1 if p.action.lower() == "buy" else -1
    signed_after  = signed_amount(p.pos_after, p.market_position)
    signed_before = signed_after - dir_ * p.contracts

    # 중복 방지: 동일 이벤트 1.5초 내 재도달 시 스킵
    key = (p.symbol, p.action, p.contracts, p.pos_after, p.market_position, p.order_price)
    now = time.time()
    if key == _last_key and (now - _last_ts) < 1.5:
        return {"ok": True, "event": "dup_skip"}
    _last_key, _last_ts = key, now

    # 1) 리버스(부호 반전) : 전량 청산 + 반대 방향 새 진입(0.6 고정)
    if not isclose(signed_before, 0.0, abs_tol=1e-9) and not same_sign(signed_before, signed_after):
        enqueue_task({
            "cmd": "close_all",
            "symbol": p.symbol,
            "mt5_symbol": mt5_symbol,
            "hint_side": "long" if signed_before > 0 else "short",
            "context": "reverse_close",
        })
        enqueue_task({
            "cmd": "entry",
            "symbol": p.symbol,
            "mt5_symbol": mt5_symbol,
            "side": "long" if signed_after > 0 else "short",
            "qty": FIXED_ENTRY_LOT,
            "context": "reverse_entry",
        })
        return {"ok": True, "event": "reverse_enqueued"}

    # 2) 새 진입: before == 0
    if isclose(signed_before, 0.0, abs_tol=1e-9):
        enqueue_task({
            "cmd": "entry",
            "symbol": p.symbol,
            "mt5_symbol": mt5_symbol,
            "side": "long" if dir_ == +1 else "short",
            "qty": FIXED_ENTRY_LOT,
            "context": "entry",
        })
        return {"ok": True, "event": "entry_enqueued"}

    # 3) 같은 방향 증액 → 무시(정책)
    if same_sign(signed_before, signed_after) and abs(signed_after) > abs(signed_before):
        return {"ok": True, "event": "pyramiding_ignored"}

    # 4) 부분 청산(분할): 같은 방향, 크기 감소
    if same_sign(signed_before, signed_after) and abs(signed_after) < abs(signed_before):
        frac = (abs(signed_before) - abs(signed_after)) / max(abs(signed_before), 1e-9)
        enqueue_task({
            "cmd": "partial_exit",
            "symbol": p.symbol,
            "mt5_symbol": mt5_symbol,
            "frac": frac,
            "context": "partial_exit",
        })
        return {"ok": True, "event": "partial_enqueued", "frac": round(frac, 6)}

    # 5) 전량 청산: after == 0
    if isclose(signed_after, 0.0, abs_tol=1e-9) and not isclose(signed_before, 0.0, abs_tol=1e-9):
        enqueue_task({
            "cmd": "close_all",
            "symbol": p.symbol,
            "mt5_symbol": mt5_symbol,
            "hint_side": "long" if signed_before > 0 else "short",
            "context": "close_all",
        })
        return {"ok": True, "event": "close_all_enqueued"}

    return {"ok": True, "event": "noop"}

def _check_agent_token(hdr_token: Optional[str]):
    if not hdr_token:
        raise HTTPException(401, "Missing X-Agent-Token")
    if hdr_token != AGENT_TOKEN:
        raise HTTPException(401, "Bad token")

@app.get("/tasks/next")
def tasks_next(x_agent_token: Optional[str] = Header(None)):
    """Windows 에이전트가 폴링해서 다음 작업을 가져감"""
    _check_agent_token(x_agent_token)
    if not TASKS:
        return {"ok": True, "task": None}
    task = TASKS.popleft()
    return {"ok": True, "task": task}

@app.post("/tasks/ack")
def tasks_ack(ack: TaskAck, x_agent_token: Optional[str] = Header(None)):
    """에이전트가 작업 결과를 알림(성공/실패 로그용)"""
    _check_agent_token(x_agent_token)
    task = PENDING.pop(ack.id, None)
    return {"ok": True, "received": bool(task), "detail": ack.detail}
