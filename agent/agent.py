import os, time, json, requests, traceback
from typing import Dict, List, Tuple
import MetaTrader5 as mt5

# ======== 환경설정 ========
RENDER_BASE   = os.environ.get("RENDER_BASE")   # 예: https://tv-mt5-hub.onrender.com
AGENT_KEY     = os.environ.get("AGENT_KEY")     # Render와 동일 값
AUTH_HEADER   = {}  # Render /pull은 별도 AUTH 불필요 (AGENT_KEY로 인증)
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.2"))

MT5_LOGIN     = int(os.environ.get("MT5_LOGIN", "87912126"))
MT5_PASSWORD  = os.environ.get("MT5_PASSWORD", "" )  # 트레이딩(마스터) 비번
MT5_SERVER    = os.environ.get("MT5_SERVER", "InfinoxLimited-MT5Live")

ENTRY_UNITS     = int(os.environ.get("ENTRY_UNITS", "3"))     # 항상 3
UNIT_LOT        = float(os.environ.get("UNIT_LOT", "0.01"))   # 1계약=0.01 lot 가정
DEVIATION_POINTS = int(os.environ.get("DEVIATION", "30"))
MAGIC           = int(os.environ.get("MAGIC", "91001"))
COMMENT         = os.environ.get("COMMENT", "AutoTV3Units")

# ======== MT5 연결 ========
def init_mt5():
    if not mt5.initialize():
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(f"MT5 login failed: {mt5.last_error()}")
    print("✅ MT5 로그인 성공")

# ======== 유틸 ========
def ensure_symbol(symbol: str):
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError(f"Symbol not found: {symbol}")
    if not info.visible:
        mt5.symbol_select(symbol, True)
    return mt5.symbol_info(symbol)

def current_price(symbol: str, side: str) -> float:
    info = mt5.symbol_info_tick(symbol)
    if side == "buy":
        return info.ask
    return info.bid

def send_deal(symbol: str, side: str, lot: float, close_ticket: int = None):
    """시장가 체결. close_ticket 있으면 해당 포지션 부분청산"""
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    price = current_price(symbol, side)
    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": DEVIATION_POINTS,
        "magic": MAGIC,
        "comment": COMMENT,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if close_ticket is not None:
        req["position"] = close_ticket  # 지정 포지션 부분청산
    result = mt5.order_send(req)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        raise RuntimeError(f"order_send failed: {result}")
    return result

def open_units(symbol: str, side: str, units: int):
    """3단 분할을 위해 아예 1단위씩 개별 포지션으로 오픈"""
    ensure_symbol(symbol)
    for _ in range(units):
        send_deal(symbol, side, UNIT_LOT)
    print(f"✅ ENTRY {side.upper()} {symbol} x{units} units (lot {UNIT_LOT} each)")

def list_positions(symbol: str, side: str) -> List[mt5.TradePosition]:
    allpos = mt5.positions_get(symbol=symbol)
    want_type = mt5.POSITION_TYPE_BUY if side == "long" else mt5.POSITION_TYPE_SELL
    items = [p for p in allpos or [] if p.type == want_type and p.magic == MAGIC]
    # 오래된 것부터 정렬(먼저 연 포지션부터 청산)
    items.sort(key=lambda x: x.time) 
    return items

def close_units(symbol: str, side: str, units: int):
    """해당 방향 포지션 중 units개(각 0.01 lot) 부분청산"""
    opp_side = "sell" if side == "long" else "buy"
    positions = list_positions(symbol, side)
    if not positions:
        print("ℹ️ close_units: no positions to close")
        return 0
    closed = 0
    for p in positions:
        if closed >= units: break
        # 각 포지션의 볼륨이 UNIT_LOT 이상이라고 가정
        lot_to_close = min(UNIT_LOT, p.volume)
        send_deal(symbol, opp_side, lot_to_close, close_ticket=p.ticket)
        closed += 1
    print(f"✅ CLOSE {symbol} {side} units={closed}")
    return closed

def close_all(symbol: str):
    """남아있는 동일 magic 모든 포지션 전량 종료"""
    pos = mt5.positions_get(symbol=symbol) or []
    count = 0
    for p in pos:
        if p.magic != MAGIC: 
            continue
        side = "sell" if p.type == mt5.POSITION_TYPE_BUY else "buy"
        send_deal(symbol, side, p.volume, close_ticket=p.ticket)
        count += 1
    print(f"✅ CLOSE-ALL {symbol} positions={count}")
    return count

# ======== 신호 파서 ========
def parse_signal(payload: dict) -> Dict:
    """
    payload 예시(TradingView Alert JSON):
      {
        "symbol": "EURUSD",
        "message": "롱진입",   # 또는 "숏진입", "분할1", "분할 2", "분할3", "손절", "롱본절", "기준이평이탈숏종료" 등
      }
    """
    text = (payload.get("message") or payload.get("msg") or payload.get("text") or "").lower()
    symbol = payload.get("symbol") or payload.get("ticker") or payload.get("sym") or ""
    symbol = symbol.strip()

    # 진입
    if any(k in text for k in ["롱진입", "long entry", "long_in", "long open", "buy entry", "go long", "long start"]):
        return {"action": "entry", "side": "buy", "symbol": symbol}
    if any(k in text for k in ["숏진입", "short entry", "short_in", "short open", "sell entry", "go short", "short start"]):
        return {"action": "entry", "side": "sell", "symbol": symbol}

    # 분할 종료 1/2/3
    if any(k in text for k in ["분할1", "tp1", "1차", "partial1", "분할 1"]):
        return {"action": "partial", "stage": 1, "symbol": symbol}
    if any(k in text for k in ["분할2", "tp2", "2차", "partial2", "분할 2"]):
        return {"action": "partial", "stage": 2, "symbol": symbol}
    if any(k in text for k in ["분할3", "tp3", "3차", "partial3", "분할 3"]):
        return {"action": "partial", "stage": 3, "symbol": symbol}

    # 전량 종료(손절/본절/기준이평이탈)
    if any(k in text for k in ["손절", "stop", "sl", "기준이평이탈숏종료", "기준이평이탈롱종료", "롱본절", "숏본절", "breakeven"]):
        return {"action": "close_all", "symbol": symbol}

    # 사이드 명시된 본절/손절(있다면 처리 보강 가능)
    return {"action": "unknown", "symbol": symbol, "raw": payload}

# ======== 실행 루프 ========
def handle(parsed: Dict):
    action = parsed.get("action")
    symbol = parsed.get("symbol")
    if not symbol:
        print("⚠️ symbol missing, skip:", parsed); return

    if action == "entry":
        side = parsed["side"]  # 'buy' or 'sell'
        # 3계약 고정 → 0.01lot씩 3개 개별 포지션 오픈
        open_units(symbol, side, ENTRY_UNITS)
        return

    if action == "partial":
        stage = parsed.get("stage")
        # 각 분할은 1계약씩 종료
        # 어느 방향 포지션인지 알아내서 닫아야 함(롱/숏 모두 지원)
        # 롱 포지션부터 닫아보고 없으면 숏 포지션 닫기
        closed = close_units(symbol, "long", 1)
        if closed == 0:
            close_units(symbol, "short", 1)
        print(f"✅ PARTIAL stage={stage} {symbol}")
        return

    if action == "close_all":
        close_all(symbol)
        return

    print("ℹ️ unknown action -> ignore:", parsed)

def poll_once() -> int:
    url = f"{RENDER_BASE}/pull"
    res = requests.post(url, json={"agent_key": AGENT_KEY, "max_batch": 10}, headers=AUTH_HEADER, timeout=10)
    res.raise_for_status()
    items = res.json().get("items", [])
    if not items:
        return 0
    ok_ids, fail_ids = [], []
    for it in items:
        try:
            payload = it["payload"]
            parsed = parse_signal(payload)
            handle(parsed)
            ok_ids.append(it["id"])
        except Exception as e:
            print("❌ handle error:", e)
            traceback.print_exc()
            fail_ids.append(it["id"])
    if ok_ids:
        requests.post(f"{RENDER_BASE}/ack", json={"agent_key": AGENT_KEY, "ids": ok_ids, "status": "done"}, timeout=10)
    if fail_ids:
        requests.post(f"{RENDER_BASE}/ack", json={"agent_key": AGENT_KEY, "ids": fail_ids, "status": "failed"}, timeout=10)
    return len(items)

def main():
    print("Starting MT5 Agent…")
    init_mt5()
    while True:
        try:
            n = poll_once()
        except Exception as e:
            print("⚠️ poll error:", e)
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
