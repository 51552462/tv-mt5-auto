# main.py
import time
from typing import Optional, Tuple
from math import isclose

from fastapi import FastAPI, Request
from pydantic import BaseModel
import uvicorn

import MetaTrader5 as mt5

# ===================== 사용자 설정 =====================
FIXED_ENTRY_LOT = 0.6        # 고정 진입 수량
BREAKEVEN_EPS = 0.0003       # 본절 데드존(0.03%)
SYMBOL_ALIASES = {
    # TV -> MT5 심볼 후보. 사용 브로커에 맞게 추가/수정
    "NQ1!": ["US100", "NAS100", "USTEC", "NAS100.cash", "US100.cash", "NAS100m", "USTECH"]
}
# =======================================================

app = FastAPI()

# ---------- 유틸 ----------
def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError("MT5 not logged in. Please login in the terminal first.")
    log(f"MT5 ok: {acc.login}, {acc.company}, mode={'hedge' if acc.trade_mode==0 else 'netting?'}")

def find_mt5_symbol(tv_symbol: str) -> Optional[str]:
    # 1) alias 매핑
    for tv, candidates in SYMBOL_ALIASES.items():
        if tv_symbol.upper() == tv.upper():
            for c in candidates:
                info = mt5.symbol_info(c)
                if info and info.visible:
                    return c
    # 2) 심볼 목록에서 키워드로 탐색(브로커마다 표기 다름)
    keywords = ["US100","NAS","USTEC","USTECH","US100.cash","NAS100","NQ"]
    all_symbols = mt5.symbols_get()
    for s in all_symbols:
        name = s.name.upper()
        if any(k in name for k in keywords):
            if s.visible:
                return s.name
    return None

def symbol_round_volume(symbol: str, vol: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return round(vol, 2)
    step = info.volume_step or 0.01
    vmin = info.volume_min or step
    vmax = info.volume_max or max(100.0, vol)
    # 스텝 스냅
    snapped = round(round(vol/step)*step, 10)
    if snapped < vmin: snapped = 0.0
    if snapped > vmax: snapped = vmax
    return snapped

def get_position_summary(symbol: str) -> Tuple[str, float, Optional[float]]:
    """
    returns (side, qty, avg_price)
    side: 'long'|'short'|'flat'
    qty : 양수 랏
    avg_price: 가중평균 진입가(없으면 None)
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None or len(positions) == 0:
        return ("flat", 0.0, None)

    long_qty = 0.0
    short_qty = 0.0
    long_val = 0.0  # price*vol 합
    short_val = 0.0

    for p in positions:
        if p.type == mt5.POSITION_TYPE_BUY:
            long_qty += p.volume
            long_val += p.price_open * p.volume
        elif p.type == mt5.POSITION_TYPE_SELL:
            short_qty += p.volume
            short_val += p.price_open * p.volume

    if long_qty > short_qty:
        avg = (long_val/long_qty) if long_qty>0 else None
        return ("long", long_qty - short_qty, avg)
    elif short_qty > long_qty:
        avg = (short_val/short_qty) if short_qty>0 else None
        return ("short", short_qty - long_qty, avg)
    else:
        return ("flat", 0.0, None)

def send_market_order(symbol: str, side: str, volume: float) -> bool:
    info = mt5.symbol_info(symbol)
    if not info:
        log(f"[ERR] symbol_info None: {symbol}")
        return False
    if not info.visible:
        mt5.symbol_select(symbol, True)

    vol = symbol_round_volume(symbol, volume)
    if vol <= 0:
        log(f"[SKIP] volume <= 0 after rounding: req={volume}")
        return True

    order_type = mt5.ORDER_TYPE_BUY if side=="long" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": vol,
        "type": order_type,
        "deviation": 50,  # 변경 가능
        "magic": 20251016,
        "comment": "tv-mt5-auto",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    if res is None:
        log(f"[ERR] order_send None: {mt5.last_error()}")
        return False
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"[ERR] order_send retcode={res.retcode}, {res.comment}")
        return False
    log(f"[OK] market {side} {vol} {symbol}")
    return True

def close_partial(symbol: str, side: str, volume: float) -> bool:
    # 부분청산 = 반대 방향 마켓 오더
    opp = "short" if side=="long" else "long"
    return send_market_order(symbol, opp, volume)

def close_all(symbol: str, side: str) -> bool:
    # 현재 보유 요약 기준 전량 반대 주문
    s, qty, _ = get_position_summary(symbol)
    if s == "flat": 
        log("[INFO] no position to close")
        return True
    if side != s:
        # 방어: 요청 side와 실제 다르면 실제 side 기준으로 닫음
        side = s
    return close_partial(symbol, side, qty)

# ---------- TV Payload ----------
class TVPayload(BaseModel):
    symbol: str
    action: str                 # "buy" | "sell"
    contracts: float
    pos_after: float            # 항상 양수(절대값)
    order_price: Optional[float] = None
    market_position: str        # "long" | "short" | "flat"
    time: Optional[str] = None

def signed_amount(abs_amount: float, market_position: str) -> float:
    if market_position == "long":
        return +abs_amount
    if market_position == "short":
        return -abs_amount
    return 0.0

# 중복 필터
_last_key = None
_last_ts = 0.0

@app.on_event("startup")
def _startup():
    init_mt5()

@app.post("/webhook")
async def webhook(req: Request):
    global _last_key, _last_ts
    data = await req.json()
    p = TVPayload(**data)

    mt5_symbol = find_mt5_symbol(p.symbol) or p.symbol
    log(f"recv: tv_symbol={p.symbol} -> mt5_symbol={mt5_symbol} | action={p.action} pos_after={p.pos_after} mpos={p.market_position}")

    dir_ = +1 if p.action.lower()=="buy" else -1
    signed_after  = signed_amount(p.pos_after, p.market_position)
    signed_before = signed_after - dir_ * p.contracts

    # 중복 방지
    key = (p.symbol, p.action, p.contracts, p.pos_after, p.market_position, p.order_price)
    now = time.time()
    if key == _last_key and (now - _last_ts) < 1.5:
        return {"ok": True, "event": "dup_skip"}
    _last_key, _last_ts = key, now

    # 로컬 보유 조회
    side_now, qty_now, avg_entry = get_position_summary(mt5_symbol)
    signed_local = 0.0 if side_now=="flat" else (qty_now if side_now=="long" else -qty_now)

    def same_sign(a,b): 
        return (a>0 and b>0) or (a<0 and b<0)

    # 1) 리버스: 부호 반전
    if not isclose(signed_before, 0.0, abs_tol=1e-9) and not same_sign(signed_before, signed_after):
        # 기존 전량 청산
        if signed_local != 0:
            close_all(mt5_symbol, "long" if signed_local>0 else "short")
        # 반대방향 새 진입(0.6 고정)
        new_side = "long" if signed_after>0 else "short"
        send_market_order(mt5_symbol, new_side, FIXED_ENTRY_LOT)
        return {"ok": True, "event": "reverse", "to": new_side}

    # 2) 새 진입: before == 0
    if isclose(signed_before, 0.0, abs_tol=1e-9):
        desired = "long" if dir_==+1 else "short"
        # 반대 보유 시 청산
        if signed_local>0 and desired=="short":
            close_all(mt5_symbol, "long")
        elif signed_local<0 and desired=="long":
            close_all(mt5_symbol, "short")
        send_market_order(mt5_symbol, desired, FIXED_ENTRY_LOT)
        return {"ok": True, "event": "entry", "side": desired}

    # 3) 같은 방향 증액 → 무시(정책)
    if same_sign(signed_before, signed_after) and abs(signed_after) > abs(signed_before):
        return {"ok": True, "event": "pyramiding_ignored"}

    # 4) 부분청산(분할): 같은 방향, 크기 감소
    if same_sign(signed_before, signed_after) and abs(signed_after) < abs(signed_before):
        if signed_local == 0:
            return {"ok": True, "event": "partial_skip_no_local_pos"}
        frac = (abs(signed_before) - abs(signed_after)) / max(abs(signed_before), 1e-9)
        my_side = "long" if signed_local>0 else "short"
        qty_to_close = symbol_round_volume(mt5_symbol, abs(signed_local) * frac)
        if qty_to_close > 0:
            close_partial(mt5_symbol, my_side, qty_to_close)
            return {"ok": True, "event": "partial_exit", "qty": qty_to_close, "frac": round(frac,6)}

    # 5) 전량 청산: after == 0
    if isclose(signed_after, 0.0, abs_tol=1e-9) and not isclose(signed_before, 0.0, abs_tol=1e-9):
        if signed_local != 0:
            # 손절/익절 라벨(선택)
            if p.order_price and avg_entry:
                if signed_local > 0:  # 롱
                    edge = (p.order_price - avg_entry)/avg_entry
                    reason = "long_take" if edge>BREAKEVEN_EPS else ("long_stop" if edge<-BREAKEVEN_EPS else "long_breakeven")
                else:                 # 숏
                    edge = (avg_entry - p.order_price)/avg_entry
                    reason = "short_take" if edge>BREAKEVEN_EPS else ("short_stop" if edge<-BREAKEVEN_EPS else "short_breakeven")
                log(f"[CLOSE ALL] label={reason} edge={edge:.5f}")
            close_all(mt5_symbol, "long" if signed_local>0 else "short")
        return {"ok": True, "event": "close_all"}

    return {"ok": True, "event": "noop"}
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
