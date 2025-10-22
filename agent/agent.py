# agent.py
# -----------------------------
# TradingView -> (Render 서버) -> MT5 자동매매 에이전트
# Windows + MetaTrader5 모듈 기반
#
# 환경변수 목록:
#   SERVER_URL          : 예) https://tv-mt5-auto.onrender.com
#   AGENT_KEY           : Render 환경변수와 동일해야 함
#   FIXED_ENTRY_LOT     : 기본 진입 랏 (예: 0.01 ~ 0.6)
#   TELEGRAM_BOT_TOKEN  : 텔레그램 봇 토큰
#   TELEGRAM_CHAT_ID    : 텔레그램 채팅 ID
#
# -----------------------------

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
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.3"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "1.0"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "10"))

FINAL_ALIASES: Dict[str, List[str]] = {
    "NQ1!": ["NAS100", "US100", "USTEC"],
    "NAS100": ["NAS100", "US100", "USTEC"],
    "US100": ["US100", "NAS100", "USTEC"],
    "USTEC": ["USTEC", "US100", "NAS100"],
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD.micro"],
}


# ============== 유틸 ==============

def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def tg(message: str):
    """텔레그램 알림"""
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

    exact = [s.name for s in all_syms if s.name.lower() == req_l]
    partial = []
    if not exact:
        for s in all_syms:
            if req_l in s.name.lower():
                partial.append(s.name)

    alias_partials = []
    aliases = FINAL_ALIASES.get(req.upper(), [])
    if aliases:
        for al in aliases:
            al_l = al.lower()
            for s in all_syms:
                name_l = s.name.lower()
                if name_l == al_l or al_l in name_l:
                    alias_partials.append(s.name)

    ordered = exact + partial + alias_partials
    seen = set()
    ordered = [x for x in ordered if not (x in seen or seen.add(x))]
    return ordered


def detect_open_symbol_from_candidates(candidates: List[str]) -> Optional[str]:
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if poss and len(poss) > 0:
            return sym
    return None


def pick_best_symbol_and_lot(requested_symbol: str, base_lot: float) -> Tuple[Optional[str], Optional[float]]:
    if not requested_symbol:
        return None, None
    req = requested_symbol.strip()
    req_l = req.lower()
    all_syms = mt5.symbols_get()
    cand_names: List[str] = []

    for s in all_syms:
        if s.name.lower() == req_l:
            cand_names.append(s.name)
    if not cand_names:
        for s in all_syms:
            if req_l in s.name.lower():
                cand_names.append(s.name)

    if not cand_names:
        alias_pool = FINAL_ALIASES.get(req.upper(), [])
        for a in alias_pool:
            a_l = a.lower()
            for s in all_syms:
                if s.name.lower() == a_l or a_l in s.name.lower():
                    cand_names.append(s.name)

    seen = set()
    cand_names = [x for x in cand_names if not (x in seen or seen.add(x))]

    acct = mt5.account_info()
    free = (acct and acct.margin_free) or 0.0

    for sym in cand_names:
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


# ============== 포지션 관련 ==============

def get_position(symbol: str) -> Tuple[str, float]:
    poss = mt5.positions_get(symbol=symbol)
    if not poss:
        return "flat", 0.0
    vol_long = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_BUY)
    vol_short = sum(p.volume for p in poss if p.type == mt5.POSITION_TYPE_SELL)
    if vol_long > 0 and vol_short == 0:
        return "long", vol_long
    if vol_short > 0 and vol_long == 0:
        return "short", vol_short
    net = vol_long - vol_short
    if abs(net) < 1e-9:
        return "flat", 0.0
    return ("long" if net > 0 else "short"), abs(net)


# ============== 주문 관련 ==============

def send_market_order(symbol: str, side: str, lot: float) -> bool:
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
    if side == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price = info.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = info.bid

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "type": order_type,
        "volume": lot,
        "price": price,
        "deviation": 50,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"[OK] market {side.upper()} {lot} {symbol}")
        tg(f"✅ ENTRY {side.upper()} {lot} {symbol}")
        return True
    else:
        log(f"[ERR] order_send ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
        tg(f"⛔ ENTRY FAIL {symbol}")
        return False


# ============== CLOSE BY ==============

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
    ok_all = True

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
                "action": mt5.TRADE_ACTION_CLOSE_BY,
                "symbol": symbol,
                "position": b.ticket,
                "position_by": s.ticket,
                "volume": qty,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            r = mt5.order_send(req)
            if r and r.retcode == mt5.TRADE_RETCODE_DONE:
                log(f"[OK] CLOSE_BY buy#{b.ticket} vs sell#{s.ticket} vol={qty}")
                remain = round(remain - qty, 10)
                s.volume = round(s.volume - qty, 10)
            else:
                ok_all = False
                log(f"[ERR] CLOSE_BY ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
    return ok_all


# ============== 청산 ==============

def _close_volume_by_tickets(symbol: str, side_now: str, vol_to_close: float) -> bool:
    if vol_to_close <= 0:
        return True
    target_type = mt5.POSITION_TYPE_BUY if side_now == "long" else mt5.POSITION_TYPE_SELL
    poss = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.type == target_type]
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
    ok_all = True

    for p in poss:
        if remain <= 0:
            break
        close_qty = min(p.volume, remain)
        close_qty = math.floor(close_qty / step) * step
        if close_qty <= 0:
            continue
        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": (mt5.ORDER_TYPE_SELL if side_now == "long" else mt5.ORDER_TYPE_BUY),
            "position": p.ticket,
            "volume": close_qty,
            "price": price,
            "deviation": 50,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        r = mt5.order_send(req)
        if r and r.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"[OK] close ticket={p.ticket} {close_qty} {symbol}")
            remain = round(remain - close_qty, 10)
        else:
            ok_all = False
            log(f"[ERR] close ticket={p.ticket} ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
    return ok_all


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


# ============== 기타 ==============

def round_down_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


def compute_fraction_for_partial(contracts: float, pos_after: float) -> float:
    before = contracts + pos_after
    if before <= 0:
        return 1.0
    return max(0.0, min(1.0, float(contracts) / float(before)))


# ============== 시그널 처리 ==============

def handle_signal(sig: dict) -> bool:
    symbol_req = (sig.get("symbol") or "").strip()
    action = (sig.get("action") or "").strip().lower()
    contracts = float(sig.get("contracts") or 0)
    pos_after = float(sig.get("pos_after") or 0)
    market_position = (sig.get("market_position") or "").strip().lower()

    cand_syms = build_candidate_symbols(symbol_req)
    open_sym = detect_open_symbol_from_candidates(cand_syms)

    if open_sym:
        mt5_symbol = open_sym
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        lot_base = max((info and info.volume_min) or FIXED_ENTRY_LOT, FIXED_ENTRY_LOT)
        lot_base = round(lot_base / step) * step
    else:
        mt5_symbol, lot_base = pick_best_symbol_and_lot(symbol_req, FIXED_ENTRY_LOT)
        if not mt5_symbol:
            log(f"[ERR] tradable symbol not found for req={symbol_req}")
            return False

    side_now, vol_now = get_position(mt5_symbol)
    log(f"[state] req={symbol_req} resolved={mt5_symbol}: now={side_now} {vol_now}lot, "
        f"action={action}, market_pos={market_position}, pos_after={pos_after}, contracts={contracts}")

    exit_intent = (market_position == "flat") or (pos_after == 0)

    if exit_intent:
        if side_now == "flat" or vol_now <= 0:
            log("[SKIP] exit-intent while flat -> ignore")
            return True
        close_by_opposites_if_any(mt5_symbol)
        return close_all(mt5_symbol)

    if side_now == "flat":
        desired = "buy" if action == "buy" else "sell"
        return send_market_order(mt5_symbol, desired, lot_base)

    if side_now == "long" and action == "sell":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts, pos_after)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        return close_partial(mt5_symbol, side_now, lot_close)

    if side_now == "short" and action == "buy":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts, pos_after)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        return close_partial(mt5_symbol, side_now, lot_close)

    log("[SKIP] same-direction or unsupported signal; no action taken")
    return True


# ============== 폴링 루프 ==============

def poll_loop():
    log(f"Agent start. server={SERVER_URL}")
    tg("🤖 MT5 Agent started")

    while True:
        try:
            payload = {"agent_key": AGENT_KEY, "max_batch": MAX_BATCH}
            res = post_json("/pull", payload)
            items = res.get("items") or []
            if not items:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            ack_ids = []
            for it in items:
                item_id = it.get("id
