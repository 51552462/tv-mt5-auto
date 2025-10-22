# agent.py
# -----------------------------
# TradingView -> (Render 서버) -> MT5 주문 실행 에이전트
# - Windows + MetaTrader5 파이썬 모듈 필요
# - 환경변수:
#     SERVER_URL          : 예) https://tv-mt5-auto.onrender.com
#     AGENT_KEY           : Render 환경변수와 동일 값
#     FIXED_ENTRY_LOT     : 기본 진입 랏(예: 0.6, 테스트는 0.01 권장)
#     TELEGRAM_BOT_TOKEN  : (선택) 텔레그램 봇 토큰
#     TELEGRAM_CHAT_ID    : (선택) 텔레그램 채팅 ID
#
# - 동작 개요:
#     1) /pull 로 신호를 가져옴 (JSON)
#     2) 현재 MT5 포지션과 신호 비교 후, 진입/분할/전량/리버스 수행
#     3) 성공/실패를 /ack 로 서버에 회신
#     4) 텔레그램으로 이벤트 알림 (선택)
#
# - 신호 포맷(예시):
#   {
#     "symbol": "NQ1!",          # 또는 NAS100/US100/USTEC 등
#     "action": "buy"|"sell",     # TV의 order.action
#     "contracts": 9,             # TV 전략이 보고한 '변동 계약수'
#     "pos_after": 6,             # 이 시그널 처리 후 포지션 계약수(전략 기준)
#     "order_price": 25048.00,
#     "market_position": "long"|"short"|"flat",
#     "time": "2025-10-19T13:10:00Z"
#   }
#
# - 분할 계산 로직:
#     before = contracts + pos_after   (TV가 보내는 값으로 역산)
#     fraction = contracts / before    (청산 또는 감축 비율)
#     close_lot = opened_lot * fraction  (opened_lot=현재 보유랏)
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


# ============== 환경변수 로드 ==============

SERVER_URL = os.environ.get("SERVER_URL", "").rstrip("/")
AGENT_KEY = os.environ.get("AGENT_KEY", "")
FIXED_ENTRY_LOT = float(os.environ.get("FIXED_ENTRY_LOT", "0.1"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "1.0"))
MAX_BATCH = int(os.environ.get("MAX_BATCH", "10"))

# 심볼 별칭 (우선순위: NAS100 -> US100 -> USTEC)
FINAL_ALIASES: Dict[str, List[str]] = {
    "NQ1!": ["NAS100", "US100", "USTEC"],
    "NAS100": ["NAS100", "US100", "USTEC"],
    "US100": ["US100", "NAS100", "USTEC"],
    "USTEC": ["USTEC", "US100", "NAS100"],
    # FX 예시(필요시 확장)
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD.micro", "EURUSD.pro"],
}


# ============== 유틸 ==============

def log(msg: str):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def tg(message: str):
    """텔레그램 알림(선택). 환경변수에 토큰/챗ID가 있어야 동작."""
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
    """MT5 터미널 연결 초기화 + 상태 출력."""
    try:
        if not mt5.initialize():
            log(f"[ERR] MT5 initialize failed: {mt5.last_error()}")
            return False
        acct = mt5.account_info()
        if not acct:
            log("[ERR] MT5 account_info None (로그인 안됐거나 연결 문제)")
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


# ============== 심볼/랏 선택 ==============
# ★ 변경: 대소문자 무시 + 부분일치(예: nas100.cash)까지 실제 심볼명을 찾아 사용

def pick_best_symbol_and_lot(requested_symbol: str, base_lot: float) -> Tuple[Optional[str], Optional[float]]:
    """
    요청 심볼을 계정의 실제 심볼명으로 해석(대소문자 무시, 부분일치 허용)한 뒤,
    min/step 스냅, 증거금 체크를 통과하는 첫 후보를 반환.
    """
    if not requested_symbol:
        return None, None

    req = requested_symbol.strip()
    req_l = req.lower()

    # 1) 모든 심볼 목록 확보
    all_syms = mt5.symbols_get()
    cand_names: List[str] = []

    # 2) 완전 대소문자 무시 동일매치
    for s in all_syms:
        if s.name.lower() == req_l:
            cand_names.append(s.name)
    # 3) 부분일치(예: nas100.cash, us100_m 등)
    if not cand_names:
        for s in all_syms:
            name_l = s.name.lower()
            if req_l in name_l:
                cand_names.append(s.name)

    # 최종 후보 없으면 별칭도 시도
    if not cand_names:
        alias_pool = FINAL_ALIASES.get(req.upper(), [])
        for a in alias_pool:
            a_l = a.lower()
            for s in all_syms:
                if s.name.lower() == a_l or a_l in s.name.lower():
                    cand_names.append(s.name)

    # 중복 제거, 순서 유지
    seen = set()
    cand_names = [x for x in cand_names if not (x in seen or seen.add(x))]

    # 증거금/스냅 체크
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

        # base_lot을 해당 심볼의 min/step으로 스냅
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

        log(f"[lot-pick] sym={sym} min={info.volume_min} step={info.volume_step} "
            f"try_lot={base_lot} snapped_lot={lot} need_margin={m} free={free}")

        if m is not None and free >= m:
            return sym, lot

    return None, None


# ============== 포지션/주문 헬퍼 ==============

def get_position(symbol: str) -> Tuple[str, float]:
    """
    현재 포지션 리턴: (side, volume)
      side: "flat"|"long"|"short"
      volume: 현재 총 랏
    """
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
    """시장가 진입: side in {'buy','sell'}"""
    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)

    if side == "buy":
        order_type = mt5.ORDER_TYPE_BUY
        price = info.ask
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = info.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "type": order_type,
        "volume": lot,
        "price": price,
        "deviation": 50,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(request)
    if r and r.retcode == mt5.TRADE_RETCODE_DONE:
        log(f"[OK] market {side.upper()} {lot} {symbol} at {r.price}")
        tg(f"✅ ENTRY {side.upper()} {lot} {symbol}")
        return True
    else:
        log(f"[ERR] order_send retcode={getattr(r,'retcode',None)}, {getattr(r,'comment', '')}")
        tg(f"⛔ ENTRY {side.upper()} {lot} {symbol} FAIL {getattr(r,'retcode',None)}")
        return False


# ========= 헤지 계정용: position 티켓 지정해 부분/전량 청산 =========

def _close_volume_by_tickets(symbol: str, side_now: str, vol_to_close: float) -> bool:
    """
    헤지 계정: 보유 포지션(여러 티켓 가능)을 순서대로 지정하여
    원하는 수량(vol_to_close)만큼 '반대 주문 + position=티켓' 으로 청산.
    """
    if vol_to_close <= 0:
        return True

    # 청산해야 할 쪽 포지션 목록 수집 (롱 보유면 BUY 타입만, 숏 보유면 SELL 타입만)
    target_type = mt5.POSITION_TYPE_BUY if side_now == "long" else mt5.POSITION_TYPE_SELL
    poss = [p for p in (mt5.positions_get(symbol=symbol) or []) if p.type == target_type]
    if not poss:
        log("[WARN] no positions to close found; skip")
        return True

    info = mt5.symbol_info(symbol)
    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        info = mt5.symbol_info(symbol)

    step = (info and info.volume_step) or 0.01
    price = (info.bid if side_now == "long" else info.ask)

    # 포지션별로 필요한 만큼 나눠서 닫기
    remain = vol_to_close
    ok_all = True

    for p in poss:
        if remain <= 0:
            break

        close_qty = min(p.volume, remain)
        # step에 맞춰 내림
        close_qty = math.floor(close_qty / step) * step
        if close_qty <= 0:
            continue

        req = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "type": (mt5.ORDER_TYPE_SELL if side_now == "long" else mt5.ORDER_TYPE_BUY),
            "position": p.ticket,     # ★ 헤지: 반드시 티켓 지정
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
            log(f"[ERR] close ticket={p.ticket} retcode={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
            # 실패해도 나머지 티켓 계속 시도

    if remain > 0:
        log(f"[WARN] remained close qty={remain} not closed")
        ok_all = False

    return ok_all


def close_partial(symbol: str, side_now: str, lot_close: float) -> bool:
    """
    부분 청산 (헤지 계정 호환): position 티켓을 지정해 필요한 수량만큼 닫는다.
    """
    if lot_close <= 0:
        log("[SKIP] close_partial non-positive")
        return True

    ok = _close_volume_by_tickets(symbol, side_now, lot_close)
    if ok:
        tg(f"🔻 PARTIAL {side_now.upper()} -{lot_close} {symbol}")
    else:
        tg(f"⛔ PARTIAL FAIL {symbol}")
    return ok


def close_all(symbol: str) -> bool:
    """
    전량 청산 (헤지 계정 호환): 해당 심볼의 모든 포지션을 티켓 지정으로 닫는다.
    """
    side_now, vol = get_position(symbol)
    if side_now == "flat" or vol <= 0:
        log("[SKIP] close_all but flat")
        return True

    ok = _close_volume_by_tickets(symbol, side_now, vol)
    if ok:
        tg(f"🧹 CLOSE ALL {symbol}")
    return ok


# ============== (보조) 스텝 내림 반올림 ==============

def round_down_to_step(x: float, step: float) -> float:
    """step 단위로 내림."""
    if step <= 0:
        return x
    return math.floor(x / step) * step + 0.0


# ============== 신호 처리 ==============

def compute_fraction_for_partial(contracts: float, pos_after: float) -> float:
    """
    TV가 보내는 'contracts'(변동분)과 'pos_after'(이후 포지션)로
    before = contracts + pos_after 로 역산 → fraction = contracts / before
    예) 9 -> 6, contracts=3, pos_after=6 => fraction=3/9=0.333..
    """
    before = contracts + pos_after
    if before <= 0:
        return 1.0
    return max(0.0, min(1.0, float(contracts) / float(before)))


# ★ 변경: 종료 의도면 무조건 '진입 금지' + 보유 시 전량 청산
def handle_signal(sig: dict) -> bool:
    """
    단일 신호 처리. True면 성공, False면 실패(재시도 가능).
    """
    symbol_req = (sig.get("symbol") or "").strip()
    action = (sig.get("action") or "").strip().lower()
    contracts = float(sig.get("contracts") or 0)
    pos_after = float(sig.get("pos_after") or 0)
    market_position = (sig.get("market_position") or "").strip().lower()

    # 1) tradable 심볼/기본 랏
    mt5_symbol, lot_base = pick_best_symbol_and_lot(symbol_req, FIXED_ENTRY_LOT)
    if not mt5_symbol:
        log(f"[ERR] tradable symbol not found for req={symbol_req} lot={FIXED_ENTRY_LOT}")
        return False

    # 2) 현재 포지션
    side_now, vol_now = get_position(mt5_symbol)
    log(f"[state] {mt5_symbol}: now={side_now} {vol_now}lot, action={action}, "
        f"market_pos={market_position}, pos_after={pos_after}, contracts={contracts}")

    # 3) 종료 의도 판정 (손절/전량 종료 신호)
    exit_intent = (market_position == "flat") or (pos_after == 0)

    # ★★ 종료 의도는 '항상' 진입을 금지하고, 보유 중이면 닫고, 없으면 무시 ★★
    if exit_intent:
        if side_now == "flat" or vol_now <= 0:
            log("[SKIP] exit-intent while flat -> ignore")
            return True
        # 보유 중이면 전량 종료
        return close_all(mt5_symbol)

    # ---- 아래부터는 '진입/분할' 로직 ----
    if side_now == "flat":
        desired = "buy" if action == "buy" else "sell"
        return send_market_order(mt5_symbol, desired, lot_base)

    if side_now == "long" and action == "sell":
        # 부분 청산
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
                item_id = it.get("id")
                sig = it.get("message") or {}
                ok = False
                try:
                    ok = handle_signal(sig)
                except Exception:
                    log("[ERR] handle_signal exception:\n" + traceback.format_exc())
                    ok = False
                if ok:
                    ack_ids.append(item_id)

            if ack_ids:
                try:
                    post_json("/ack", {"agent_key": AGENT_KEY, "ids": ack_ids})
                except Exception:
                    log("[ERR] ack failed:\n" + traceback.format_exc())

            time.sleep(0.2)

        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 401:
                log("[ERR] poll: 401 Unauthorized (AGENT_KEY 불일치 가능)")
            else:
                log(f"[ERR] poll HTTP {status}: {e}")
            time.sleep(2.0)

        except Exception as e:
            log("[ERR] poll exception: " + str(e))
            time.sleep(2.0)


# ============== 메인 ==============

def main():
    # 사전 점검
    if not SERVER_URL or not AGENT_KEY:
        log("[FATAL] SERVER_URL/AGENT_KEY 환경변수를 설정하세요.")
        return

    if not ensure_mt5_initialized():
        return

    # 건강상태 로그
    h = get_health()
    if h:
        log(f"health: {h}")

    # 루프 시작
    poll_loop()


if __name__ == "__main__":
    main()
