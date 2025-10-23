# agent.py
# --------------------------------------------------------------------
# TradingView → Render 서버 → MT5 자동매매 에이전트
# - 종료(손절/전량) 신호에서 신규 진입 금지(티켓 지정 DEAL + CLOSE_BY)
# - /pull 응답이 signal 또는 payload(또는 항목 자체)여도 파싱
# - 심볼 누락/이상 시 NAS100 계열(US100/USTEC) 자동 탐색하여 진입 가능
# - exit 판정: market_position=="flat" 또는 action in EXIT_ACTIONS 만 종료로 간주
# --------------------------------------------------------------------

import os
import time
import json
import math
import traceback
from typing import Optional, Tuple, Dict, Any, List

import requests
import MetaTrader5 as mt5


# ============== 환경변수 ==============
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AGENT_KEY = os.environ.get("AGENT_KEY", "")
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.1"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "1.0"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "10"))

# 심볼 별칭(브로커마다 이름이 다름)
FINAL_ALIASES: Dict[str, List[str]] = {
    "NQ1!":   ["NAS100", "US100", "USTEC"],
    "NAS100": ["NAS100", "US100", "USTEC"],
    "US100":  ["US100", "NAS100", "USTEC"],
    "USTEC":  ["USTEC", "US100", "NAS100"],
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD.micro"],
}


# ============== 유틸 ==============
def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def tg(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print("[TG ERR]", e, flush=True)


def ensure_mt5_initialized() -> bool:
    try:
        if not mt5.initialize():
            log(f"[ERR] MT5 initialize failed: {mt5.last_error()}")
            return False
        acct = mt5.account_info()
        if not acct:
            log("[ERR] MT5 account_info None")
            return False
        log(f"MT5 ok: {acct.login}, {acct.company}")
        return True
    except Exception:
        log("[ERR] MT5 initialize exception:\n" + traceback.format_exc())
        return False


def post_json(path: str, payload: dict, timeout: float = 10.0) -> dict:
    url = f"{SERVER_URL}{path}"
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_health() -> dict:
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ============== 심볼 탐지 ==============
def build_candidate_symbols(requested_symbol: str) -> List[str]:
    req = (requested_symbol or "").strip()
    if not req:
        return []
    req_l = req.lower()
    all_syms = mt5.symbols_get() or []

    # 1) 정확 일치
    exact = [s.name for s in all_syms if s.name.lower() == req_l]

    # 2) 부분 일치
    partial = []
    if not exact:
        for s in all_syms:
            if req_l in s.name.lower():
                partial.append(s.name)

    # 3) 별칭 기반 부분 일치
    alias_partials = []
    aliases = FINAL_ALIASES.get(req.upper(), [])
    for al in aliases:
        al_l = al.lower()
        for s in all_syms:
            nm = s.name.lower()
            if nm == al_l or al_l in nm:
                alias_partials.append(s.name)

    ordered = exact + partial + alias_partials
    seen = set()
    return [x for x in ordered if not (x in seen or seen.add(x))]


def detect_open_symbol_from_candidates(candidates: List[str]) -> Optional[str]:
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if poss and len(poss) > 0:
            return sym
    return None


def detect_any_open_from_alias_pool() -> Optional[str]:
    """심볼 누락시 NAS100 계열에서 열려있는 심볼 자동 탐색."""
    for base in ["NAS100", "US100", "USTEC"]:
        cands = build_candidate_symbols(base)
        sym = detect_open_symbol_from_candidates(cands)
        if sym:
            return sym
    return None


def pick_best_symbol_and_lot(requested_symbol: str, base_lot: float) -> Tuple[Optional[str], Optional[float]]:
    """진입 상황에서만: 요청 심볼/부분일치/별칭 후보 중 진입 가능한 심볼과 랏 선택."""
    if not requested_symbol:
        return None, None
    req = requested_symbol.strip()
    req_l = req.lower()
    all_syms = mt5.symbols_get() or []
    cand = []

    for s in all_syms:
        if s.name.lower() == req_l:
            cand.append(s.name)
    if not cand:
        for s in all_syms:
            if req_l in s.name.lower():
                cand.append(s.name)
    if not cand:
        for a in FINAL_ALIASES.get(req.upper(), []):
            a_l = a.lower()
            for s in all_syms:
                nm = s.name.lower()
                if nm == a_l or a_l in nm:
                    cand.append(s.name)

    seen = set()
    cand = [x for x in cand if not (x in seen or seen.add(x))]

    acct = mt5.account_info()
    free = (acct and acct.margin_free) or 0.0

    for sym in cand:
        info = mt5.symbol_info(sym)
        if not info:
            continue
        if not info.visible:
            mt5.symbol_select(sym, True)
            info = mt5.symbol_info(sym)
            if not info or not info.visible:
                continue

        lot = max(info.volume_min, base_lot)
        step = info.volume_step or 0.01
        lot = round(lot / step) * step
        lot = max(lot, info.volume_min)
        if info.volume_max and lot > info.volume_max:
            lot = info.volume_max

        price = info.ask or info.bid
        if not price:
            continue

        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, sym, lot, price)
        if m is None:
            m = mt5.order_calc_margin(mt5.ORDER_TYPE_SELL, sym, lot, price)

        log(f"[lot-pick] sym={sym} need_margin={m} free={free}")
        if m is not None and free >= m:
            return sym, lot

    return None, None


# ============== 포지션/주문 ==============
def get_position(symbol: str) -> Tuple[str, float]:
    poss = mt5.positions_get(symbol=symbol)
    if not poss:
        return "flat", 0.0
    vL = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
    vS = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
    if vL > 0 and vS == 0:
        return "long", vL
    if vS > 0 and vL == 0:
        return "short", vS
    net = vL - vS
    if abs(net) < 1e-9:
        return "flat", 0.0
    return ("long" if net > 0 else "short"), abs(net)


def send_market_order(symbol: str, side: str, lot: float) -> bool:
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    price = info.ask if side == "buy" else info.bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "type": (mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL),
        "volume": lot,
        "price": price,
        "deviation": 50,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"[OK] market {side} {lot} {symbol}")
        tg(f"✅ ENTRY {side.upper()} {lot} {symbol}")
        return True
    log(f"[ERR] order_send ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
    tg(f"⛔ ENTRY FAIL {symbol}")
    return False


# ============== CLOSE_BY/청산 ==============
def close_by_opposites_if_any(symbol: str) -> bool:
    poss = mt5.positions_get(symbol=symbol) or []
    buys = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
    sells = [p for p in poss if p.type == mt5.POSITION_TYPE_SELL]
    if not buys or not sells:
        return True

    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    step = (info and info.volume_step) or 0.01
    ok = True
    for b in buys:
        remain = b.volume
        for s in sells:
            if remain <= 0:
                break
            if s.volume <= 0:
                continue
            qty = min(remain, s.volume)
            qty = math.floor(qty / step) * step
            if qty <= 0:
                continue
            req = {
                "action": mt5.TRADE_ACTION_CLOSE_BY,  # ← 여기 따옴표 오류 수정 완료
                "symbol": symbol,
                "position": b.ticket,
                "position_by": s.ticket,
                "volume": qty,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"[OK] CLOSE_BY b#{b.ticket} vs s#{s.ticket} vol={qty}")
                remain = round(remain - qty, 10)
                s.volume = round(s.volume - qty, 10)
            else:
                ok = False
                log(f"[ERR] CLOSE_BY ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
    return ok


def _close_volume_by_tickets(symbol: str, side_now: str, vol_to_close: float) -> bool:
    if vol_to_close <= 0:
        return True
    ttype = mt5.POSITION_TYPE_BUY if side_now == "long" else mt5.POSITION_TYPE_SELL
    poss = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.type == ttype]
    if not poss:
        log("[WARN] no positions to close")
        return True

    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    step = (info and info.volume_step) or 0.01
    price = (info.bid if side_now == "long" else info.ask)
    remain = vol_to_close
    ok = True

    for p in poss:
        if remain <= 0:
            break
        qty = min(p.volume, remain)
        qty = math.floor(qty / step) * step
        if qty <= 0:
            continue
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": (mt5.ORDER_TYPE_SELL if side_now == "long" else mt5.ORDER_TYPE_BUY),
            "position": p.ticket,           # ← 티켓 지정: 신규반대 진입 방지
            "volume": qty,
            "price": price,
            "deviation": 50,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"[OK] close ticket={p.ticket} {qty} {symbol}")
            remain = round(remain - qty, 10)
        else:
            ok = False
            log(f"[ERR] close ticket={p.ticket} ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
    return ok


def close_partial(symbol: str, side_now: str, lot_close: float) -> bool:
    if lot_close <= 0:
        return True
    ok = _close_volume_by_tickets(symbol, side_now, lot_close)
    if ok:
        tg(f"🔻 PARTIAL {side_now.upper()} -{lot_close} {symbol}")
    return ok


def close_all(symbol: str) -> bool:
    side_now, vol = get_position(symbol)
    if side_now == "flat" or vol <= 0:
        return True
    ok = _close_volume_by_tickets(symbol, side_now, vol)
    if ok:
        tg(f"🧹 CLOSE ALL {symbol}")
    return ok


def close_all_for_candidates(candidates: List[str]) -> bool:
    anything = False
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if not poss:
            continue
        try:
            close_by_opposites_if_any(sym)
        except Exception:
            log("[WARN] CLOSE_BY error:\n" + traceback.format_exc())
        try:
            s, v = get_position(sym)
            if s != "flat" and v > 0:
                _ = close_all(sym)
                anything = True
        except Exception:
            log("[WARN] close_all error:\n" + traceback.format_exc())
    return True if anything or True else True


# ============== 보조 ==============
def round_down_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


def compute_fraction_for_partial(contracts: float, pos_after: float) -> float:
    before = (contracts or 0.0) + (pos_after or 0.0)
    if before <= 0:
        return 1.0
    return max(0.0, min(1.0, float(contracts or 0.0) / float(before)))


# ============== 시그널 처리 ==============
EXIT_ACTIONS = {"close", "exit", "flat", "stop", "sl", "tp", "close_all"}

def _read_symbol_from_signal(sig: dict) -> str:
    # 다양한 키 허용 (혹시 다른 키로 들어와도 대응)
    for k in ["symbol", "sym", "ticker", "SYMBOL", "Symbol", "s"]:
        v = sig.get(k)
        if v:
            return str(v).strip()
    return ""


def handle_signal(sig: dict) -> bool:
    # 입력 파싱
    symbol_req = _read_symbol_from_signal(sig)  # ← 심볼이 없으면 "" 반환
    action = str(sig.get("action", "")).strip().lower()
    contracts = sig.get("contracts", None)
    try:
        contracts = float(contracts) if contracts is not None and str(contracts).strip() != "" else None
    except:
        contracts = None

    pos_after_raw = sig.get("pos_after", None)
    try:
        pos_after = float(pos_after_raw) if pos_after_raw is not None and str(pos_after_raw).strip() != "" else None
    except:
        pos_after = None

    market_position = str(sig.get("market_position", "")).strip().lower()

    # 후보 구성 + 열린 심볼 우선
    cand_syms = build_candidate_symbols(symbol_req) if symbol_req else []
    open_sym = detect_open_symbol_from_candidates(cand_syms) if cand_syms else detect_any_open_from_alias_pool()
    if open_sym:
        mt5_symbol = open_sym
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        lot_base = max((info and info.volume_min) or FIXED_ENTRY_LOT, FIXED_ENTRY_LOT)
        lot_base = round(lot_base / step) * step
    else:
        # 진입 신규 심볼 선택(심볼 미전달 시 NAS100 계열에서 선택)
        base_req = symbol_req if symbol_req else "NAS100"
        mt5_symbol, lot_base = pick_best_symbol_and_lot(base_req, FIXED_ENTRY_LOT)
        if not mt5_symbol:
            log(f"[ERR] tradable symbol not found for req={symbol_req}")
            return False

    side_now, vol_now = get_position(mt5_symbol)
    log(f"[state] req={symbol_req} resolved={mt5_symbol}: now={side_now} {vol_now}lot, "
        f"action={action}, market_pos={market_position}, pos_after={pos_after}, contracts={contracts}")

    # 종료 의도: market_position=='flat' 또는 action이 종료 계열
    exit_intent = (market_position == "flat") or (action in EXIT_ACTIONS)

    # 종료 신호 처리 (절대 신규 진입 금지)
    if exit_intent:
        targets = cand_syms if cand_syms else build_candidate_symbols(mt5_symbol)
        close_all_for_candidates(targets)

        s, v = get_position(mt5_symbol)
        if s != "flat" and v > 0:
            close_by_opposites_if_any(mt5_symbol)
            return close_all(mt5_symbol)

        log("[SKIP] exit-intent handled (flat/closed)")
        return True

    # 진입/분할 처리
    if side_now == "flat":
        if action not in ("buy", "sell"):
            log("[SKIP] unknown action for flat state")
            return True
        desired = "buy" if action == "buy" else "sell"
        return send_market_order(mt5_symbol, desired, lot_base)

    if side_now == "long" and action == "sell":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts or 0.0, pos_after or vol_now)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    if side_now == "short" and action == "buy":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts or 0.0, pos_after or vol_now)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    log("[SKIP] same-direction or unsupported signal; no action taken")
    return True


# ============== 폴링 루프 ==============
def poll_loop():
    log(f"Agent start. server={SERVER_URL}")
    tg("🤖 MT5 Agent started")

    while True:
        try:
            res = post_json("/pull", {"agent_key": AGENT_KEY, "max_batch": MAX_BATCH})
            items = res.get("items") or []
            if not items:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            ack_ids = []
            for it in items:
                item_id = it.get("id")
                # 서버가 signal/payload/그 자체 형태 어느 것이든 수용
                sig = it.get("signal") or it.get("payload") or it

                ok = False
                try:
                    ok = handle_signal(sig)
                except Exception:
                    log("[ERR] handle_signal exception:\n" + traceback.format_exc())
                    ok = False

                if ok and item_id is not None:
                    ack_ids.append(item_id)

            if ack_ids:
                try:
                    post_json("/ack", {"agent_key": AGENT_KEY, "ids": ack_ids})
                except Exception:
                    log("[WARN] ack failed")

        except Exception:
            log("[ERR] poll_loop exception:\n" + traceback.format_exc())
            time.sleep(POLL_INTERVAL_SEC)


# ============== main ==============
def main():
    if not SERVER_URL or not AGENT_KEY:
        log("[FATAL] SERVER_URL/AGENT_KEY env missing")
        return
    if not ensure_mt5_initialized():
        return
    log(f"server health: {json.dumps(get_health())}")
    poll_loop()


if __name__ == "__main__":
    main()
