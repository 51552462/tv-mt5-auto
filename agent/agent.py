# /agent/agent.py
import os
import time
import requests
from typing import Optional, Tuple

import MetaTrader5 as mt5

# ===================== 사용자 설정 =====================
SERVER_URL   = os.environ.get("SERVER_URL",   "http://127.0.0.1:8000")
AGENT_TOKEN  = os.environ.get("AGENT_TOKEN",  "set-me")
POLL_SEC     = float(os.environ.get("POLL_SEC", "0.8"))

# 에이전트 현지(브로커)에 맞춘 최종 심볼 후보
FINAL_ALIASES = {
    "NQ1!": ["US100", "NAS100", "US100.cash", "NAS100.cash", "USTEC", "USTECH"]
}
# =======================================================

def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)

def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError("MT5 not logged in. Please login in the terminal first.")
    log(f"MT5 ok: {acc.login}, {acc.company}, 'hedge' if acc.trade_mode==0 else 'netting?'")

def resolve_symbol(tv_symbol: str, server_mt5_symbol: Optional[str]) -> str:
    # 1) 서버가 추천한 심볼 우선
    if server_mt5_symbol:
        info = mt5.symbol_info(server_mt5_symbol)
        if info:
            if not info.visible:
                mt5.symbol_select(server_mt5_symbol, True)
            return server_mt5_symbol
    # 2) 로컬 후보
    for tv, candidates in FINAL_ALIASES.items():
        if tv_symbol.upper() == tv.upper():
            for c in candidates:
                info = mt5.symbol_info(c)
                if info:
                    if not info.visible:
                        mt5.symbol_select(c, True)
                    return c
    return tv_symbol

def symbol_round_volume(symbol: str, vol: float) -> float:
    info = mt5.symbol_info(symbol)
    if not info:
        return round(vol, 2)
    step = info.volume_step or 0.01
    vmin = info.volume_min or step
    vmax = info.volume_max or max(100.0, vol)
    snapped = round(round(vol/step)*step, 10)
    if snapped < vmin: snapped = 0.0
    if snapped > vmax: snapped = vmax
    return snapped

def get_position_summary(symbol: str) -> Tuple[str, float, float | None]:
    """
    returns (side, qty, avg_price)
    side: 'long'|'short'|'flat'
    qty : 양수 랏
    """
    positions = mt5.positions_get(symbol=symbol)
    if positions is None or len(positions) == 0:
        return ("flat", 0.0, None)

    long_qty = 0.0
    short_qty = 0.0
    long_val = 0.0
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
        log(f"[SKIP] volume<=0 after rounding: req={volume}")
        return True

    order_type = mt5.ORDER_TYPE_BUY if side=="long" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": vol,
        "type": order_type,
        "deviation": 50,
        "magic": 20251016,
        "comment": "tv-mt5-agent",
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
    opp = "short" if side == "long" else "long"
    return send_market_order(symbol, opp, volume)

def close_all(symbol: str, side_hint: Optional[str] = None) -> bool:
    s, qty, _ = get_position_summary(symbol)
    if s == "flat":
        log("[INFO] no position to close")
        return True
    return close_partial(symbol, s, qty)

def poll_next_task() -> Optional[dict]:
    try:
        r = requests.get(
            f"{SERVER_URL}/tasks/next",
            headers={"X-Agent-Token": AGENT_TOKEN},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("task")
    except Exception as e:
        log(f"[ERR] poll_next_task: {e}")
        return None

def ack_task(task_id: int, ok: bool, detail: str = ""):
    try:
        r = requests.post(
            f"{SERVER_URL}/tasks/ack",
            headers={"X-Agent-Token": AGENT_TOKEN},
            json={"id": task_id, "ok": ok, "detail": detail},
            timeout=10,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"[ERR] ack_task: {e}")

def run_loop():
    init_mt5()
    log(f"Agent start. server={SERVER_URL}")

    while True:
        task = poll_next_task()
        if not task:
            time.sleep(float(os.environ.get("POLL_SEC", "0.8")))
            continue

        tid = task["id"]
        cmd = task.get("cmd")
        tv_symbol = task.get("symbol")
        mt5_symbol = resolve_symbol(tv_symbol, task.get("mt5_symbol"))

        ok, detail = True, "ok"
        try:
            if cmd == "entry":
                side = task["side"]
                qty  = float(task["qty"])
                ok = send_market_order(mt5_symbol, side, qty)
                detail = f"entry {side} {qty}"

            elif cmd == "partial_exit":
                frac = float(task["frac"])
                side_now, qty_now, _ = get_position_summary(mt5_symbol)
                if side_now == "flat":
                    ok, detail = True, "no position"
                else:
                    close_qty = symbol_round_volume(mt5_symbol, qty_now * frac)
                    ok = close_partial(mt5_symbol, side_now, close_qty)
                    detail = f"partial {close_qty} ({frac:.4f})"

            elif cmd == "close_all":
                ok = close_all(mt5_symbol, task.get("hint_side"))
                detail = "close_all"

            else:
                ok, detail = True, f"noop({cmd})"

        except Exception as e:
            ok, detail = False, f"exception: {e}"

        ack_task(tid, ok, detail)

if __name__ == "__main__":
    run_loop()
