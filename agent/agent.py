# agent.py
# --------------------------------------------------------------------
# TradingView(또는 수동 테스트) → Render 서버 → MT5 자동매매 에이전트
# - 심볼 대소문자 문제/별칭 자동 처리
# - 종료(손절/전량) 신호에서 절대 신규 진입 금지
# - 헤지 계정 티켓 기반 청산(TRADE_ACTION_DEAL + position)
# - 양방 포지션 동시 존재 시 CLOSE_BY 우선 상쇄
# - 텔레그램 알림(선택)
#
# 필요 환경변수:
#   SERVER_URL            예) https://tv-mt5-auto.onrender.com
#   AGENT_KEY             Render 서버와 동일한 키
#   FIXED_ENTRY_LOT       기본 진입 랏 (예: 0.01 ~ 0.6)
#   TELEGRAM_BOT_TOKEN    (선택) 텔레그램 봇 토큰
#   TELEGRAM_CHAT_ID      (선택) 텔레그램 채팅 ID
#   POLL_INTERVAL_SEC     (선택) 기본 1.0
#   MAX_BATCH             (선택) 기본 10
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

# 심볼 별칭(브로커마다 이름이 달라 후보다중 탐색)
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
    """텔레그램 알림(선택)"""
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
    """
    요청 심볼과 별칭, 전체 심볼 목록을 바탕으로
    대소문자 무시/부분일치 후보 목록을 만들어 반환.
    (우선순위: 정확일치 -> 부분일치 -> 별칭 기반 부분일치)
    """
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
    if aliases:
        for al in aliases:
            al_l = al.lower()
            for s in all_syms:
                name_l = s.name.lower()
                if name_l == al_l or al_l in name_l:
                    alias_partials.append(s.name)

    ordered = exact + partial + alias_partials

    # 중복 제거, 순서 유지
    seen = set()
    ordered = [x for x in ordered if not (x in seen or seen.add(x))]
    return ordered


def detect_open_symbol_from_candidates(candidates: List[str]) -> Optional[str]:
    """후보 중 실제로 포지션이 열려있는 심볼을 찾아 반환."""
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if poss and len(poss) > 0:
            return sym
    return None


def pick_best_symbol_and_lot(requested_symbol: str, base_lot: float) -> Tuple[Optional[str], Optional[float]]:
    """
    포지션이 없을 때(진입 상황)만 사용.
    요청 심볼/부분일치/별칭 후보를 돌며
    계좌 free margin으로 들어갈 수 있는 최소 랏을 고른다.
    """
    if not requested_symbol:
        return None, None
    req = requested_symbol.strip()
    req_l = req.lower()
    all_syms = mt5.symbols_get() or []
    cand_names: List[str] = []

    # 정확 일치
    for s in all_syms:
        if s.name.lower() == req_l:
            cand_names.append(s.name)
    # 부분 일치
    if not cand_names:
        for s in all_syms:
            if req_l in s.name.lower():
                cand_names.append(s.name)
    # 별칭
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


# ============== 포지션/주문 ==============

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


def send_market_order(symbol: str, side: str, lot: float) -> bool:
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
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
        log(f"[OK] market {side} {lot} {symbol}")
        tg(f"✅ ENTRY {side.upper()} {lot} {symbol}")
        return True
    else:
        log(f"[ERR] order_send ret={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
        tg(f"⛔ ENTRY FAIL {symbol}")
        return False


# ============== CLOSE_BY 및 청산 ==============

def close_by_opposites_if_any(symbol: str) -> bool:
    """
    같은 심볼에 BUY/SELL 동시 존재 시 가능한 범위 CLOSE_BY로 상쇄.
    """
    poss = mt5.positions_get(symbol=symbol) or []
    buys  = [p for p in poss if p.type == mt5.POSITION_TYPE_BUY]
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


def _close_volume_by_tickets(symbol: str, side_now: str, vol_to_close: float) -> bool:
    """티켓 단위 부분/전량 청산(헤지 계정 안전 방식)"""
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
            "position": p.ticket,  # ← 반드시 티켓 지정해서 반대 신규 진입 방지
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


def close_all_for_candidates(candidates: List[str]) -> bool:
    """
    종료(손절/전량) 신호에서 사용.
    후보 심볼들을 전부 훑어보면서 실제로 열려있는 심볼을 찾아
    1) CLOSE_BY 상쇄
    2) 남은 잔량 전량 청산
    무엇이라도 닫으면 True. 실패가 있어도 전체적으로 True 반환(신호 소비 목적).
    """
    anything = False
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if not poss:
            continue
        try:
            close_by_opposites_if_any(sym)
        except Exception:
            log("[WARN] close_by_opposites_if_any error:\n" + traceback.format_exc())
        try:
            _side, _vol = get_position(sym)
            if _side != "flat" and _vol > 0:
                _ = close_all(sym)
                anything = True
        except Exception:
            log("[WARN] close_all error:\n" + traceback.format_exc())
    return True if anything or True else True  # 항상 True


# ============== 보조 ==============

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
    """
    단일 신호 처리
    입력키:
      symbol, action("buy"/"sell"), contracts, pos_after, market_position("long"/"short"/"flat")
    """
    symbol_req = (sig.get("symbol") or "").strip()
    action = (sig.get("action") or "").strip().lower()
    contracts = float(sig.get("contracts") or 0)
    pos_after = float(sig.get("pos_after") or 0)
    market_position = (sig.get("market_position") or "").strip().lower()

    # 후보 구성 + 열린 심볼 우선
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

    # ─────────────────────────────────────────────
    # 종료(손절/전량) 신호: 진입 금지 + 후보 전수 스캔해 전량 청산
    # ─────────────────────────────────────────────
    if exit_intent:
        # 후보 전체에서 열려있는 심볼 닫기(CLOSE_BY → 전량)
        close_all_for_candidates(cand_syms)

        # 그래도 현재 결정 심볼에 잔량이 남았으면 추가로 닫기
        side_now, vol_now = get_position(mt5_symbol)
        if side_now != "flat" and vol_now > 0:
            close_by_opposites_if_any(mt5_symbol)
            return close_all(mt5_symbol)

        log("[SKIP] exit-intent handled (flat/closed)")
        return True

    # 아래부터는 진입/분할 처리
    if side_now == "flat":
        desired = "buy" if action == "buy" else "sell"
        return send_market_order(mt5_symbol, desired, lot_base)

    if side_now == "long" and action == "sell":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts, pos_after)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    if side_now == "short" and action == "buy":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        frac = compute_fraction_for_partial(contracts, pos_after)
        lot_close = round_down_to_step(vol_now * frac, step)
        lot_close = min(max(lot_close, step), vol_now)
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    log("[SKIP] same-direction or unsupported signal; no action taken")
    return True


# ============== 폴링 루프(서버 연동) ==============

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
                item_id = it.get("id")

                # ★★★ 핵심 수정: 서버가 'payload'로 줄 수도 있고, 'signal'로 줄 수도 있음
                #               (둘 다 없으면 항목 자체를 신호로 간주)
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
    h = get_health()
    log(f"server health: {json.dumps(h)}")
    poll_loop()


if __name__ == "__main__":
    main()
