# agent.py
# --------------------------------------------------------------------
# TradingView → Render 서버 → MT5 자동매매 에이전트
# - 종료(손절/전량) 신호에서 신규 진입 금지(티켓 지정 DEAL + CLOSE_BY)
# - /pull 응답이 signal 또는 payload(또는 항목 자체)여도 파싱
# - 심볼 누락 시 NAS100 계열(US100/USTEC) 자동 탐색
# - FIXED_ENTRY_LOT는 스텝에 '올림(ceil)'으로 맞춰 최소 지정 랏을 보장
# - REQUIRE_MARGIN_CHECK=1 이면 마진 부족 시 스텝 단위로 낮춤
# - NO_MONEY(10019) 시 스텝 다운 재시도 + split-entry로 목표 랏 충족
# --------------------------------------------------------------------

import os
import time
import json
import math
import traceback
from typing import Optional, Tuple, Dict, Any, List

import requests
import MetaTrader5 as mt5

# ── HTTP resilient session ────────────────────────────────────────────────
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_http_retry = Retry(
    total=5,
    backoff_factor=0.8,                # 0.8, 1.6, 2.4 …
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
_http = requests.Session()
_http.mount("http://",  HTTPAdapter(max_retries=_http_retry))
_http.mount("https://", HTTPAdapter(max_retries=_http_retry))
# ─────────────────────────────────────────────────────────────────────────


# ============== 환경변수 ==============
SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AGENT_KEY = os.environ.get("AGENT_KEY", "")
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.01"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "1.0"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "10"))

REQUIRE_MARGIN_CHECK = os.environ.get("REQUIRE_MARGIN_CHECK", "0").strip() in ("1","true","True","YES","yes")
ALLOW_SPLIT_ENTRIES = os.environ.get("ALLOW_SPLIT_ENTRIES", "1").strip() in ("1","true","True","YES","yes")

DEFAULT_SYMBOL = os.environ.get("DEFAULT_SYMBOL", "").strip()

STRICT_FIXED_MODE = os.environ.get("STRICT_FIXED_MODE", "0").strip() in ("1","true","True","YES","yes")

PARTIAL_LOT = os.environ.get("PARTIAL_LOT", "").strip()
PARTIAL_LOT = float(PARTIAL_LOT) if PARTIAL_LOT else None

IGNORE_SIGNAL_CONTRACTS = os.environ.get("IGNORE_SIGNAL_CONTRACTS", "1").strip() in ("1","true","True","YES","yes")


# ===========================
# 심볼 별칭 (BTC + NAS + ETH)
# ===========================
FINAL_ALIASES: Dict[str, List[str]] = {
    "NQ1!":   ["NAS100", "US100", "USTEC"],
    "NAS100": ["NAS100", "US100", "USTEC"],
    "US100":  ["US100", "NAS100", "USTEC"],
    "USTEC":  ["USTEC", "US100", "NAS100"],
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD.micro"],

    # 비트코인
    "BTCUSD":  ["BTCUSD", "BTCUSDT", "BTCUSD.m", "BTCUSD.micro", "BTCUSD.a", "XBTUSD"],
    "BTCUSDT": ["BTCUSDT", "BTCUSD", "BTCUSD.m", "BTCUSD.micro", "XBTUSD"],

    # 이더리움
    "ETHUSD":  ["ETHUSD", "ETHUSDT", "ETHUSD.m", "ETHUSDmicro", "XETUSD", "XETHUSD"],
    "ETHUSDT": ["ETHUSDT", "ETHUSD", "XETUSD", "XETHUSD", "ETHUSD.m", "ETHUSDmicro"],
    "XETUSD":  ["XETUSD", "ETHUSD", "ETHUSDT", "ETHUSD.m", "ETHUSDmicro"],
}


# ===========================
# 기본 함수 / 유틸
# ===========================
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


def post_json(path: str, payload: dict, timeout: float = 20.0) -> dict:
    url = f"{SERVER_URL}{path}"
    try:
        r = _http.post(url, json=payload, timeout=timeout, headers={"Connection": "keep-alive"})
        r.raise_for_status()
        return r.json()
    except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
        log(f"[WARN] post_json timeout {path}: {e}")
        return {}
    except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError, requests.exceptions.HTTPError) as e:
        log(f"[WARN] post_json conn/http err {path}: {e}")
        return {}
    except Exception as e:
        log(f"[ERR] post_json fatal {path}: {e}")
        return {}


def get_health() -> dict:
    try:
        r = _http.get(f"{SERVER_URL}/health", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return {}


# ===========================
# 심볼 탐색  ← ★ 수정: .crp 등 변형을 끝순위로
# ===========================
def build_candidate_symbols(requested_symbol: str) -> List[str]:
    req = (requested_symbol or "").strip()
    if not req:
        return []
    req_l = req.lower()
    all_syms = mt5.symbols_get() or []

    # 1) 정확히 같은 이름
    exact = [s.name for s in all_syms if s.name.lower() == req_l]

    # 2) 부분 일치(요청 문자열이 포함된 심볼)
    partial = []
    if not exact:
        for s in all_syms:
            if req_l in s.name.lower():
                partial.append(s.name)

    # 3) 별칭들 중 '정확히 같은 이름' 우선
    aliases = FINAL_ALIASES.get(req.upper(), [])
    alias_exact = []
    alias_partial = []
    for al in aliases:
        al_l = al.lower()
        for s in all_syms:
            nm = s.name.lower()
            if nm == al_l:
                alias_exact.append(s.name)
            elif al_l in nm:
                alias_partial.append(s.name)

    # 4) 정렬: .crp 같은 변형은 맨 뒤로 (가끔 브로커가 비활성/테스트용으로 둠)
    ordered = exact + alias_exact + partial + alias_partial
    seen = set()
    ordered = [x for x in ordered if not (x in seen or seen.add(x))]

    # 우선순위: 점(.) 없는 심볼 > .m/.micro 등 일반 변형 > .crp 같은 특수 변형
    def rank(name: str) -> tuple:
        n = name.lower()
        bad = (".crp" in n or ".test" in n or ".demo" in n)
        dotted = ("." in n)
        return (bad, dotted, len(n))  # bad=True면 뒤로, 그다음 dotted, 마지막 길이

    ordered.sort(key=rank)
    return ordered


def detect_open_symbol_from_candidates(candidates: List[str]) -> Optional[str]:
    for sym in candidates:
        poss = mt5.positions_get(symbol=sym)
        if poss and len(poss) > 0:
            return sym
    return None


def detect_any_open_from_alias_pool() -> Optional[str]:
    bases = []
    if DEFAULT_SYMBOL:
        bases.append(DEFAULT_SYMBOL)
    bases += ["BTCUSD", "BTCUSDT", "NAS100", "US100", "USTEC", "ETHUSD", "ETHUSDT", "XETUSD"]
    for base in bases:
        cands = build_candidate_symbols(base)
        sym = detect_open_symbol_from_candidates(cands)
        if sym:
            return sym
    return None


# ============== 보조 ==============
def ceil_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.ceil(x / step) * step


def floor_to_step(x: float, step: float) -> float:
    if step <= 0:
        return x
    return math.floor(x / step) * step


# ============== 랏 결정 ==============
def _decide_lot_no_margin(info, base_lot: float) -> float:
    step = info.volume_step or 0.01
    vol_min = info.volume_min or step
    vol_max = info.volume_max or 0.0

    desired = max(vol_min, base_lot)
    lot = ceil_to_step(desired, step)

    if vol_max and lot > vol_max:
        lot = floor_to_step(vol_max, step)

    return max(vol_min, lot)


def _decide_lot_with_margin(symbol: str, info, base_lot: float) -> float:
    step = info.volume_step or 0.01
    vol_min = info.volume_min or step
    vol_max = info.volume_max or 0.0

    desired = max(vol_min, base_lot)
    lot = ceil_to_step(desired, step)

    price = info.ask or info.bid
    acct = mt5.account_info()
    free = (acct and acct.margin_free) or 0.0

    def enough(qty: float) -> bool:
        if not price:
            return True
        m = mt5.order_calc_margin(mt5.ORDER_TYPE_BUY, symbol, qty, price)
        if m is None:
            m = mt5.order_calc_margin(mt5.ORDER_TYPE_SELL, symbol, qty, price)
        return (m is None) or (free >= m)

    test = lot
    if vol_max and test > vol_max:
        test = floor_to_step(vol_max, step)

    while test >= vol_min and not enough(test):
        test = round(floor_to_step(test - step, step), 10)

    return max(vol_min, test)


def pick_best_symbol_and_lot(requested_symbol: str, base_lot: float) -> Tuple[Optional[str], Optional[float]]:
    if not requested_symbol:
        req = DEFAULT_SYMBOL or "NAS100"
    else:
        req = requested_symbol
    req = req.strip()
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

    # ★ 여기서도 동일 기준으로 정렬
    def rank(name: str) -> tuple:
        n = name.lower()
        bad = (".crp" in n or ".test" in n or ".demo" in n)
        dotted = ("." in n)
        return (bad, dotted, len(n))

    cand.sort(key=rank)

    for sym in cand:
        info = mt5.symbol_info(sym)
        if not info:
            continue
        if not info.visible:
            mt5.symbol_select(sym, True)
            info = mt5.symbol_info(sym)
            if not info or not info.visible:
                continue

        if REQUIRE_MARGIN_CHECK:
            lot = _decide_lot_with_margin(sym, info, base_lot)
        else:
            lot = _decide_lot_no_margin(info, base_lot)

        step = info.volume_step or 0.01
        vol_min = info.volume_min or step
        log(f"[lot-pick] sym={sym} step={step} min={vol_min} base={base_lot} => lot={lot}")
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


def _send_deal(symbol: str, side: str, volume: float) -> tuple:
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    price = info.ask if side == "buy" else info.bid
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "type": order_type,
        "volume": volume,
        "price": price,
        "deviation": 50,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
        return True, r.retcode, getattr(r, "comment", "")
    return False, getattr(r, "retcode", None), getattr(r, "comment", "")


def send_market_order(symbol: str, side: str, lot: float) -> bool:
    """
    1) lot 시도 → NO_MONEY면 step씩 줄여 재시도(최소 vol_min).
    2) 최종 체결량이 목표 미달이고 ALLOW_SPLIT_ENTRIES=1 이면
       vol_min씩 반복 체결하여 목표 lot까지 채운다.
    """
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    step = (info and info.volume_step) or 0.01
    vol_min = (info and info.volume_min) or step

    target = max(vol_min, lot)
    attempt = target
    filled = 0.0

    # (1) 스텝 다운 재시도 루프
    while attempt >= vol_min:
        ok, ret, cmt = _send_deal(symbol, side, attempt)
        if ok:
            filled += attempt
            log(f"[OK] market {side} {attempt} {symbol} (filled={filled}/{target})")
            break
        log(f"[ERR] order_send ret={ret} {cmt} (try vol={attempt})")
        if ret == mt5.TRADE_RETCODE_NO_MONEY:
            attempt = round(floor_to_step(attempt - step, step), 10)
            continue
        else:
            tg(f"⛔ ENTRY FAIL {symbol} ret={ret} {cmt}")
            return False

    # (2) split-entry로 목표 채우기
    if ALLOW_SPLIT_ENTRIES and filled < target:
        remain = round(target - filled, 10)
        while remain >= vol_min - 1e-12:
            piece = min(vol_min, remain)
            ok, ret, cmt = _send_deal(symbol, side, piece)
            if not ok:
                log(f"[WARN] split fail ret={ret} {cmt} (piece={piece}, filled={filled})")
                if ret == mt5.TRADE_RETCODE_NO_MONEY:
                    break
                else:
                    break
            filled = round(filled + piece, 10)
            remain = round(target - filled, 10)
            log(f"[OK] split {side} {piece} {symbol} (filled={filled}/{target})")

    if filled > 0:
        tg(f"✅ ENTRY {side.upper()} {filled} {symbol} (target {target})")
        return True

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
                "action": mt5.TRADE_ACTION_CLOSE_BY,
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
            "position": p.ticket,           # 티켓 지정: 신규 반대진입 방지
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


# ============== 시그널 처리 ==============
EXIT_ACTIONS = {"close", "exit", "flat", "stop", "sl", "tp", "close_all"}

def _read_symbol_from_signal(sig: dict) -> str:
    for k in ["symbol", "sym", "ticker", "SYMBOL", "Symbol", "s"]:
        v = sig.get(k)
        if v:
            return str(v).strip()
    return ""


def handle_signal(sig: dict) -> bool:
    symbol_req = _read_symbol_from_signal(sig)
    if not symbol_req and DEFAULT_SYMBOL:
        symbol_req = DEFAULT_SYMBOL

    action = str(sig.get("action", "")).strip().lower()

    contracts = sig.get("contracts", None)
    try:
        contracts = float(contracts) if (contracts is not None and str(contracts).strip() != "") else None
    except:
        contracts = None
    if IGNORE_SIGNAL_CONTRACTS:
        contracts = None

    pos_after_raw = sig.get("pos_after", None)
    try:
        pos_after = float(pos_after_raw) if pos_after_raw is not None and str(pos_after_raw).strip() != "" else None
    except:
        pos_after = None

    market_position = str(sig.get("market_position", "")).strip().lower()

    cand_syms = build_candidate_symbols(symbol_req) if symbol_req else []
    open_sym = detect_open_symbol_from_candidates(cand_syms) if cand_syms else detect_any_open_from_alias_pool()
    if open_sym:
        mt5_symbol = open_sym
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        vol_min = (info and info.volume_min) or step
        desired = max(vol_min, FIXED_ENTRY_LOT)
        lot_base = ceil_to_step(desired, step)
        log(f"[lot-base] resolved={mt5_symbol} step={step} min={vol_min} FIXED={FIXED_ENTRY_LOT} -> {lot_base}")
    else:
        base_req = symbol_req if symbol_req else (DEFAULT_SYMBOL or "NAS100")
        mt5_symbol, lot_base = pick_best_symbol_and_lot(base_req, FIXED_ENTRY_LOT)
        if not mt5_symbol:
            log(f"[ERR] tradable symbol not found for req={symbol_req}")
            return False

    side_now, vol_now = get_position(mt5_symbol)
    log(f"[state] req={symbol_req} resolved={mt5_symbol}: now={side_now} {vol_now}lot, "
        f"action={action}, market_pos={market_position}, pos_after={pos_after}, contracts={contracts}, STRICT={STRICT_FIXED_MODE}")

    # === 전량 종료 의도 ===
    exit_intent = (market_position == "flat") or (action in EXIT_ACTIONS) or (pos_after == 0)
    if exit_intent:
        targets = cand_syms if cand_syms else build_candidate_symbols(mt5_symbol)
        close_all_for_candidates(targets)
        s, v = get_position(mt5_symbol)
        if s != "flat" and v > 0:
            close_by_opposites_if_any(mt5_symbol)
            return close_all(mt5_symbol)
        log("[SKIP] exit-intent handled (flat/closed)")
        return True

    # === STRICT_FIXED_MODE: 고정 랏/분할 랏만 사용 ===
    if STRICT_FIXED_MODE:
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        partial_lot = PARTIAL_LOT if (PARTIAL_LOT and PARTIAL_LOT > 0) else (FIXED_ENTRY_LOT if FIXED_ENTRY_LOT > 0 else step)

        if side_now == "flat":
            if action not in ("buy", "sell"):
                log("[SKIP] unknown action for flat state (STRICT)")
                return True
            desired_side = "buy" if action == "buy" else "sell"
            return send_market_order(mt5_symbol, desired_side, lot_base)

        if side_now == "long":
            if action == "sell":
                lot_close = min(vol_now, max(step, partial_lot))
                return close_partial(mt5_symbol, side_now, lot_close)
            elif action == "buy":
                return send_market_order(mt5_symbol, "buy", lot_base)
            else:
                log("[SKIP] unsupported action (STRICT, long)")
                return True

        if side_now == "short":
            if action == "buy":
                lot_close = min(vol_now, max(step, partial_lot))
                return close_partial(mt5_symbol, side_now, lot_close)
            elif action == "sell":
                return send_market_order(mt5_symbol, "sell", lot_base)
            else:
                log("[SKIP] unsupported action (STRICT, short)")
                return True

        return True

    # === STRICT 모드가 아닐 때
    if side_now == "flat":
        if action not in ("buy", "sell"):
            log("[SKIP] unknown action for flat state]")
            return True
        desired_side = "buy" if action == "buy" else "sell"
        return send_market_order(mt5_symbol, desired_side, lot_base)

    if side_now == "long" and action == "sell":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        base = (contracts or 0.0) + (pos_after or vol_now)
        frac = (contracts or 0.0) / base if base > 0 else 1.0
        lot_close = max(step, min(vol_now, math.floor((vol_now * frac) / step) * step))
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    if side_now == "short" and action == "buy":
        info = mt5.symbol_info(mt5_symbol)
        step = (info and info.volume_step) or 0.01
        base = (contracts or 0.0) + (pos_after or vol_now)
        frac = (contracts or 0.0) / base if base > 0 else 1.0
        lot_close = max(step, min(vol_now, math.floor((vol_now * frac) / step) * step))
        if lot_close <= 0:
            log("[INFO] calc close_qty <= 0 -> skip")
            return True
        return close_partial(mt5_symbol, side_now, lot_close)

    log("[SKIP] same-direction or unsupported signal; no action taken")
    return True


# ============== 폴링 루프 ==============
def poll_loop():
    log(f"env FIXED_ENTRY_LOT={FIXED_ENTRY_LOT} REQUIRE_MARGIN_CHECK={REQUIRE_MARGIN_CHECK} ALLOW_SPLIT_ENTRIES={ALLOW_SPLIT_ENTRIES}")
    log(f"env STRICT_FIXED_MODE={STRICT_FIXED_MODE} PARTIAL_LOT={PARTIAL_LOT} DEFAULT_SYMBOL='{DEFAULT_SYMBOL}' IGNORE_SIGNAL_CONTRACTS={IGNORE_SIGNAL_CONTRACTS}")
    log(f"Agent start. server={SERVER_URL}")
    tg("🤖 MT5 Agent started")

    import random
    tick = 0
    consec_fail = 0

    while True:
        tick += 1
        # 주기적 keep-alive
        if tick % 100 == 0:
            _ = get_health()

        try:
            res = post_json("/pull", {"agent_key": AGENT_KEY, "max_batch": MAX_BATCH})
            items = res.get("items") or []
            if not items:
                time.sleep(POLL_INTERVAL_SEC + random.random()*0.7)
                consec_fail = 0
                continue

            ack_ids = []
            for it in items:
                item_id = it.get("id")
                sig = it.get("signal") or it.get("payload") or it
                ok = False
                try:
                    ok = handle_signal(sig)
                except Exception as e:
                    log(f"[ERR] handle_signal: {e}\n" + traceback.format_exc())
                    ok = False
                if ok and item_id is not None:
                    ack_ids.append(item_id)

            if ack_ids:
                _ = post_json("/ack", {"agent_key": AGENT_KEY, "ids": ack_ids})
            consec_fail = 0
        except Exception as e:
            log(f"[WARN] poll_loop exception: {e}")
            consec_fail += 1
            backoff = min(30.0, (1.5 ** consec_fail))   # 최대 30초
            time.sleep(backoff)
            continue

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
