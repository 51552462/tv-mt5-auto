# /agent/agent.py
import os
import time
import json
import requests
from typing import Optional, Tuple

import MetaTrader5 as mt5

# ===================== 사용자 설정 =====================
SERVER_URL   = os.environ.get("SERVER_URL",   "https://<your-render-domain>")
AGENT_KEY    = os.environ.get("AGENT_KEY",    "set-me")
POLL_SEC     = float(os.environ.get("POLL_SEC", "0.8"))

# 고정 진입 수량(랏) – 분할은 비율 환산으로 처리
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.6"))

# 브로커 최종 심볼 후보(네 브로커 표기에 맞게 필요하면 추가)
FINAL_ALIASES = {
    "NQ1!": ["US100", "NAS100", "US100.cash", "NAS100.cash", "USTEC", "USTECH"]
}
# =======================================================


def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


# --------------- MT5 유틸 ---------------
def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    acc = mt5.account_info()
    if acc is None:
        raise RuntimeError("MT5 not logged in. Please login in terminal first.")
    log(f"MT5 ok: {acc.login}, {acc.company}")

def resolve_symbol(tv_symbol: str, server_hint: Optional[str]) -> str:
    # 1) 서버가 추천한 심볼(있다면) 우선
    if server_hint:
        info = mt5.symbol_info(server_hint)
        if info:
            if not info.visible:
                mt5.symbol_select(server_hint, True)
            return server_hint
    # 2) 로컬 후보 탐색
    for tv, candidates in FINAL_ALIASES.items():
        if tv_symbol.upper() == tv.upper():
            for c in candidates:
                info = mt5.symbol_info(c)
                if info:
                    if not info.visible:
                        mt5.symbol_select(c, True)
                    return c
    # 3) 실패 시 TV 심볼 그대로(대개 존재하지 않음)
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
    if not positions:
        return ("flat", 0.0, None)

    long_qty = 0.0
    short_qty = 0.0
    long_val = 0.0
    short_val = 0.0

    for p in positions:
        if p.type == mt5.POSITION_TYPE_BUY:
            long_qty  += p.volume
            long_val  += p.price_open * p.volume
        elif p.type == mt5.POSITION_TYPE_SELL:
            short_qty += p.volume
            short_val += p.price_open * p.volume

    if long_qty > short_qty:
        avg = (long_val/long_qty) if long_qty > 0 else None
        return ("long", long_qty - short_qty, avg)
    elif short_qty > long_qty:
        avg = (short_val/short_qty) if short_qty > 0 else None
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

    order_type = mt5.ORDER_TYPE_BUY if side == "long" else mt5.ORDER_TYPE_SELL
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": vol,
        "type": order_type,
        "deviation": 50,
        "magic": 20251019,
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

def close_all(symbol: str) -> bool:
    s, qty, _ = get_position_summary(symbol)
    if s == "flat":
        log("[INFO] no position to close")
        return True
    return close_partial(symbol, s, qty)


# --------------- Render 통신 ---------------
def pull_batch(max_batch: int = 5) -> list[dict]:
    try:
        r = requests.post(
            f"{SERVER_URL}/pull",
            json={"agent_key": AGENT_KEY, "max_batch": max_batch},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return items
    except Exception as e:
        log(f"[ERR] pull: {e}")
        return []

def ack(ids: list[int], status: str = "done"):
    if not ids:
        return
    try:
        r = requests.post(
            f"{SERVER_URL}/ack",
            json={"agent_key": AGENT_KEY, "ids": ids, "status": status},
            timeout=15,
        )
        r.raise_for_status()
    except Exception as e:
        log(f"[ERR] ack: {e}")


# --------------- TV 메시지 판별 로직 ---------------
def signed_amount(abs_amount: float, market_position: str) -> float:
    if market_position == "long":
        return +abs_amount
    if market_position == "short":
        return -abs_amount
    return 0.0

def handle_tv_payload(tv: dict, symbol_hint: Optional[str]) -> bool:
    """
    tv = {
      "symbol": "NQ1!",
      "action": "buy"|"sell",
      "contracts": 9,
      "pos_after": 9,
      "order_price": 25044.75,
      "market_position": "short"|"long"|"flat",
      "time": "2025-10-16T10:47:00Z"
    }
    """
    # 필수 필드 체크
    for k in ("symbol","action","contracts","pos_after","market_position"):
        if k not in tv:
            log(f"[WARN] missing field {k} in payload: {tv}")
            return True  # ack 처리하여 큐 정체 방지(원하면 False 처리)

    tv_symbol  = str(tv["symbol"])
    action     = str(tv["action"]).lower()          # buy/sell
    contracts  = float(tv["contracts"])
    pos_after  = float(tv["pos_after"])
    mpos       = str(tv["market_position"]).lower() # long/short/flat
    order_px   = float(tv["order_price"]) if "order_price" in tv and tv["order_price"] is not None else None

    dir_ = +1 if action == "buy" else -1
    signed_after  = signed_amount(pos_after, mpos)
    signed_before = signed_after - dir_ * contracts

    mt5_symbol = resolve_symbol(tv_symbol, symbol_hint)
    side_now, qty_now, avg_price = get_position_summary(mt5_symbol)

    def same_sign(a: float, b: float) -> bool:
        return (a > 0 and b > 0) or (a < 0 and b < 0)

    # 1) 리버스: 부호 반전(전량 청산 + 반대 방향 새 진입)
    if abs(signed_before) > 1e-9 and not same_sign(signed_before, signed_after):
        if side_now != "flat":
            close_all(mt5_symbol)
        new_side = "long" if signed_after > 0 else "short"
        return send_market_order(mt5_symbol, new_side, FIXED_ENTRY_LOT)

    # 2) 새 진입: before == 0
    if abs(signed_before) < 1e-9:
        desired = "long" if dir_ == +1 else "short"
        # 반대 보유 시 정리
        if side_now == "long" and desired == "short":
            close_all(mt5_symbol)
        elif side_now == "short" and desired == "long":
            close_all(mt5_symbol)
        return send_market_order(mt5_symbol, desired, FIXED_ENTRY_LOT)

    # 3) 같은 방향 증액 → 무시(정책)
    if same_sign(signed_before, signed_after) and abs(signed_after) > abs(signed_before):
        log("[INFO] pyramiding signal ignored")
        return True

    # 4) 부분 청산(분할): 같은 방향, 크기 감소
    if same_sign(signed_before, signed_after) and abs(signed_after) < abs(signed_before):
        if side_now == "flat":
            log("[INFO] local flat; skip partial")
            return True
        frac = (abs(signed_before) - abs(signed_after)) / max(abs(signed_before), 1e-9)
        close_qty = symbol_round_volume(mt5_symbol, qty_now * frac)
        if close_qty > 0:
            return close_partial(mt5_symbol, side_now, close_qty)
        log("[INFO] calc close_qty <= 0; skip")
        return True

    # 5) 전량 청산: after == 0
    if abs(signed_after) < 1e-9 and abs(signed_before) > 0:
        if side_now != "flat":
            return close_all(mt5_symbol)
        return True

    log("[INFO] noop")
    return True


# --------------- 메인 루프 ---------------
def run_loop():
    init_mt5()
    log(f"Agent start. server={SERVER_URL}")

    while True:
        batch = pull_batch(max_batch=5)
        if not batch:
            time.sleep(POLL_SEC)
            continue

        ok_ids, fail_ids = [], []

        for item in batch:
            tid  = item["id"]
            data = item.get("payload") or {}
            # 서버에서 심볼 힌트를 넣어줄 수도 있으므로 같이 받음
            mt5_hint = data.get("mt5_symbol") if isinstance(data.get("mt5_symbol"), str) else None

            try:
                # TradingView 원문(JSON)일 수도 있고, 문자열일 수도 있어 안전하게 처리
                payload = data
                if isinstance(data, str):
                    try:
                        payload = json.loads(data)
                    except Exception:
                        payload = {"raw": data}

                # TV 포맷일 때만 처리
                if all(k in payload for k in ("symbol","action","contracts","pos_after","market_position")):
                    ok = handle_tv_payload(payload, mt5_hint)
                else:
                    # 그 외 포맷은 패스(혹은 필요 시 별도 처리)
                    log(f"[INFO] skip unsupported payload: {payload}")
                    ok = True

                (ok_ids if ok else fail_ids).append(tid)
            except Exception as e:
                log(f"[ERR] task {tid} exception: {e}")
                fail_ids.append(tid)

        if ok_ids:
            ack(ok_ids, status="done")
        if fail_ids:
            ack(fail_ids, status="failed")


if __name__ == "__main__":
    run_loop()
