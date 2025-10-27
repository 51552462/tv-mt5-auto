# -*- coding: utf-8 -*-
"""
MetaTrader5 Windows Agent for TradingView → Render(FastAPI) → MT5 live trading

- Keeps original NAS100 pipeline intact.
- ADDED: DEFAULT_SYMBOL, BTC aliases, BTC-first symbol detection fallback.
- Accepts flexible signal payloads: symbol|sym|ticker|s, action, contracts, pos_after, market_position, order_price, time.
- Supports partial close by target "pos_after" or incremental "contracts".
- Optional margin check and split entries.

Author: tv-mt5-auto
"""

import os
import sys
import time
import json
import math
import queue
import logging
from typing import Dict, List, Optional, Tuple

import requests
import MetaTrader5 as mt5

# -----------------------------
# Logging
# -----------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("agent")

# -----------------------------
# Environment
# -----------------------------
SERVER_URL = os.environ.get("SERVER_URL", "").strip().rstrip("/")
if not SERVER_URL:
    log.error("SERVER_URL env is required.")
    sys.exit(1)

AGENT_KEY = os.environ.get("AGENT_KEY", "").strip()
if not AGENT_KEY:
    log.error("AGENT_KEY env is required.")
    sys.exit(1)

# ADDED: 기본 심볼 설정 (트뷰 알림에 심볼이 없을 때 사용)
DEFAULT_SYMBOL = os.environ.get("DEFAULT_SYMBOL", "").strip()

# optional features
REQUIRE_MARGIN_CHECK = os.environ.get("REQUIRE_MARGIN_CHECK", "0").strip() == "1"
ALLOW_SPLIT_ENTRIES = os.environ.get("ALLOW_SPLIT_ENTRIES", "1").strip() == "1"

# optional fixed entry lot
FIXED_ENTRY_LOT = os.environ.get("FIXED_ENTRY_LOT", "").strip()
FIXED_ENTRY_LOT = float(FIXED_ENTRY_LOT) if FIXED_ENTRY_LOT else None

POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "2").strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

# -----------------------------
# Symbol Aliases (kept + BTC added)
# -----------------------------
# NOTE: Keep NAS aliases intact; just add BTC ones.
FINAL_ALIASES: Dict[str, List[str]] = {
    "NQ1!":   ["NQ1!", "NAS100", "US100", "USTEC"],
    "NAS100": ["NAS100", "US100", "USTEC", "NAS100.m", "US100.m", "USTEC.m"],
    "US100":  ["US100", "NAS100", "USTEC", "US100.m", "NAS100.m", "USTEC.m"],
    "USTEC":  ["USTEC", "US100", "NAS100", "USTEC.m", "US100.m", "NAS100.m"],
    "EURUSD": ["EURUSD", "EURUSD.m", "EURUSD.micro"],

    # ADDED: BTC aliases (broad coverage for broker suffixes)
    "BTCUSD":  ["BTCUSD", "BTCUSD.m", "BTCUSDmicro", "BTCUSD.a", "BTCUSDT", "XBTUSD"],
    "BTCUSDT": ["BTCUSDT", "BTCUSD", "BTCUSD.m", "BTCUSDmicro", "XBTUSD"],
}

# -----------------------------
# Telegram helper (optional)
# -----------------------------
def tg_send(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=5,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")

# -----------------------------
# MT5 helpers
# -----------------------------
def mt5_init() -> bool:
    if mt5.initialize():
        log.info("MT5 initialized.")
        return True
    log.error(f"MT5 initialize failed: {mt5.last_error()}")
    return False

def get_account_info():
    acc = mt5.account_info()
    if acc is None:
        log.error(f"account_info failed: {mt5.last_error()}")
    return acc

def symbol_select(sym: str) -> bool:
    if mt5.symbol_select(sym, True):
        return True
    log.debug(f"symbol_select({sym}) failed, last_error={mt5.last_error()}")
    return False

def get_symbol_info(sym: str):
    info = mt5.symbol_info(sym)
    return info

def normalize_volume(sym: str, volume: float) -> float:
    info = get_symbol_info(sym)
    if not info:
        return volume
    step = info.volume_step or 0.01
    minv = info.volume_min or step
    maxv = info.volume_max or max(minv, 100.0)
    # clip & round to step
    if volume <= 0:
        return 0.0
    q = math.floor((volume + 1e-12) / step) * step
    q = max(minv, min(q, maxv))
    # extra fix due to FP rounding
    q = round(q / step) * step
    q = round(q, 2) if step >= 0.01 else round(q, 4)
    return q

def get_position_volume(sym: str) -> Tuple[float, float]:
    """
    Returns (long_volume, short_volume) in lots for given symbol.
    """
    total_long = 0.0
    total_short = 0.0
    positions = mt5.positions_get(symbol=sym)
    if positions:
        for p in positions:
            if p.type == mt5.POSITION_TYPE_BUY:
                total_long += p.volume
            elif p.type == mt5.POSITION_TYPE_SELL:
                total_short += p.volume
    return total_long, total_short

def ensure_symbol_ready(sym: str) -> bool:
    if not symbol_select(sym):
        return False
    info = get_symbol_info(sym)
    if not info or not info.visible:
        log.error(f"Symbol not visible or no info: {sym}")
        return False
    return True

def send_market_order(sym: str, order_type: int, volume: float, comment: str = "") -> bool:
    if volume <= 0:
        return True
    if not ensure_symbol_ready(sym):
        return False
    price = get_symbol_info(sym).ask if order_type == mt5.ORDER_TYPE_BUY else get_symbol_info(sym).bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": sym,
        "volume": volume,
        "type": order_type,
        "price": price,
        "deviation": 50,
        "magic": 123456,
        "comment": comment[:25],
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    if REQUIRE_MARGIN_CHECK:
        check = mt5.order_check(request)
        if check is None:
            log.error(f"order_check None: {mt5.last_error()}")
            return False
        if check.retcode != mt5.TRADE_RETCODE_DONE:
            log.warning(f"order_check failed ret={check.retcode} vol={volume} {sym}")
            return False

    res = mt5.order_send(request)
    if res is None:
        log.error(f"order_send None: {mt5.last_error()}")
        return False
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log.warning(f"order_send ret={res.retcode} vol={volume} {sym} comment={comment}")
        return False
    return True

def split_exec(sym: str, order_type: int, total_vol: float, step: float) -> bool:
    """
    Execute total_vol in chunks of 'step' (last remainder once).
    """
    if total_vol <= 0:
        return True
    n_full = int(total_vol // step)
    rem = round(total_vol - n_full * step, 6)
    # execute full chunks
    for _ in range(n_full):
        if not send_market_order(sym, order_type, step, comment="split"):
            return False
        time.sleep(0.05)
    # execute remainder
    if rem > 1e-9:
        if not send_market_order(sym, order_type, rem, comment="split-rem"):
            return False
    return True

def apply_order(sym: str, direction: str, volume: float) -> bool:
    """
    direction: "buy" or "sell"
    volume: lots
    Splits if needed.
    """
    vol = normalize_volume(sym, volume)
    if vol <= 0:
        return True
    order_type = mt5.ORDER_TYPE_BUY if direction == "buy" else mt5.ORDER_TYPE_SELL

    if not ensure_symbol_ready(sym):
        return False

    info = get_symbol_info(sym)
    step = info.volume_step or 0.01
    if ALLOW_SPLIT_ENTRIES and vol > step * 1.5:
        return split_exec(sym, order_type, vol, step)
    return send_market_order(sym, order_type, vol, comment="market")

# -----------------------------
# Signal parsing & symbol resolution
# -----------------------------
def read_symbol_from_signal(sig: dict) -> str:
    """
    Accept variety: symbol | sym | ticker | s
    """
    for k in ("symbol", "sym", "ticker", "s"):
        v = str(sig.get(k, "")).strip()
        if v:
            return v
    return ""

def build_candidate_symbols(base: str) -> List[str]:
    b = base.upper()
    cands = [b]
    if b in FINAL_ALIASES:
        for a in FINAL_ALIASES[b]:
            if a not in cands:
                cands.append(a)
    # add broker typical suffixes if not present
    extra = []
    for x in cands:
        extra.extend([x + ".m", x + ".micro", x + ".a"])  # generic guesses
    for e in extra:
        if e not in cands:
            cands.append(e)
    return cands

def detect_open_symbol_from_candidates(cands: List[str]) -> Optional[str]:
    """
    Returns first visible symbol among candidates.
    """
    for s in cands:
        info = get_symbol_info(s)
        if info and (info.visible or symbol_select(s)):
            return s
    return None

def detect_any_open_from_alias_pool() -> Optional[str]:
    """
    ADDED: DEFAULT_SYMBOL and BTC-first fallback, then NAS.
    Keeps NAS behavior intact while enabling BTC quickly.
    """
    bases: List[str] = []
    if DEFAULT_SYMBOL:
        bases.append(DEFAULT_SYMBOL.upper())

    # ADDED: BTC 우선 탐색
    bases += ["BTCUSD", "BTCUSDT", "NAS100", "US100", "USTEC"]

    seen = set()
    for base in bases:
        if base in seen:
            continue
        seen.add(base)
        cands = build_candidate_symbols(base)
        sym = detect_open_symbol_from_candidates(cands)
        if sym:
            return sym
    return None

def resolve_symbol(sig: dict) -> Optional[str]:
    """
    1) from signal fields
    2) DEFAULT_SYMBOL
    3) auto-detect (BTC→NAS)
    """
    s = read_symbol_from_signal(sig)
    if s:
        cands = build_candidate_symbols(s)
        found = detect_open_symbol_from_candidates(cands)
        if found:
            return found
        # fallback to raw
        return s.upper()

    if DEFAULT_SYMBOL:
        cands = build_candidate_symbols(DEFAULT_SYMBOL)
        found = detect_open_symbol_from_candidates(cands)
        if found:
            return found
        return DEFAULT_SYMBOL.upper()

    found = detect_any_open_from_alias_pool()
    if found:
        return found

    # last resort
    return "BTCUSD"

# -----------------------------
# Position targeting logic
# -----------------------------
def target_by_pos_after(sym: str, pos_after: float, hint_side: Optional[str]) -> bool:
    """
    Move current net position to pos_after (lots).
    hint_side: "long"|"short"|"flat"|None -> informational
    For hedging accounts, we approximate to net via closing opposite, opening toward target.
    """
    pos_after = max(0.0, pos_after)
    info = get_symbol_info(sym)
    step = info.volume_step or 0.01

    long_vol, short_vol = get_position_volume(sym)
    net = long_vol - short_vol  # +long, -short
    cur_net = net

    tgt_net = round(pos_after, 8) if (hint_side in (None, "", "long", "flat")) else (
        -round(pos_after, 8) if hint_side == "short" else 0.0
    )
    if hint_side == "flat":
        tgt_net = 0.0

    # If the signal included market_position explicitly:
    # long -> tgt positive, short -> tgt negative, flat -> 0
    mp = str((hint_side or "")).lower()
    if mp == "long":
        tgt_net = abs(pos_after)
    elif mp == "short":
        tgt_net = -abs(pos_after)
    elif mp == "flat":
        tgt_net = 0.0

    delta = tgt_net - cur_net
    delta = round(delta, 8)

    if abs(delta) < (step * 0.5):
        log.info(f"[pos_after] {sym} already near target net={cur_net:.4f} → {tgt_net:.4f}")
        return True

    if delta > 0:
        # need to BUY delta
        return apply_order(sym, "buy", abs(delta))
    else:
        # need to SELL abs(delta)
        return apply_order(sym, "sell", abs(delta))

def apply_by_contracts(sym: str, action: str, contracts: float) -> bool:
    """
    "contracts" : incremental lots to add/remove by direction.
    action: "buy" or "sell"
    """
    contracts = max(0.0, contracts)
    if contracts == 0:
        return True
    direction = "buy" if action.lower() == "buy" else "sell"
    return apply_order(sym, direction, contracts)

# -----------------------------
# HTTP Pull
# -----------------------------
def pull_signals() -> List[dict]:
    """
    POST /pull with agent key
    Response: {"signals":[ {...}, {...} ]}
    """
    url = f"{SERVER_URL}/pull"
    try:
        r = requests.post(url, json={"agent_key": AGENT_KEY}, timeout=10)
        if r.status_code != 200:
            log.warning(f"/pull {r.status_code}: {r.text[:200]}")
            return []
        data = r.json()
        sigs = data.get("signals", [])
        if not isinstance(sigs, list):
            return []
        return sigs
    except Exception as e:
        log.warning(f"/pull error: {e}")
        return []

# -----------------------------
# Signal handling
# -----------------------------
def parse_action(sig: dict) -> str:
    a = str(sig.get("action", "")).lower().strip()
    if a in ("buy", "long", "open_long"):
        return "buy"
    if a in ("sell", "short", "open_short"):
        return "sell"
    # if market_position exists and pos_after changes, action can be derived,
    # but we default to 'buy' to avoid ignoring a valid instruction.
    return "buy"

def parse_market_position(sig: dict) -> Optional[str]:
    mp = str(sig.get("market_position", "")).lower().strip()
    if mp in ("long", "short", "flat"):
        return mp
    return None

def parse_contracts(sig: dict) -> Optional[float]:
    if "contracts" in sig:
        try:
            return float(sig["contracts"])
        except Exception:
            return None
    return None

def parse_pos_after(sig: dict) -> Optional[float]:
    if "pos_after" in sig:
        try:
            return float(sig["pos_after"])
        except Exception:
            return None
    return None

def handle_signal(sig: dict) -> bool:
    try:
        sym_req = read_symbol_from_signal(sig)
        # ADDED: 기본 심볼/ BTC fallback
        if not sym_req:
            sym_req = DEFAULT_SYMBOL or "BTCUSD"

        sym = resolve_symbol(sig | {"symbol": sym_req})
        action = parse_action(sig)
        pos_after = parse_pos_after(sig)
        contracts = parse_contracts(sig)
        market_pos = parse_market_position(sig)

        order_px = sig.get("order_price")
        tstamp = sig.get("time")

        log.info(f"Signal: sym={sym} act={action} pos_after={pos_after} contracts={contracts} mp={market_pos} px={order_px} t={tstamp}")

        if pos_after is not None:
            ok = target_by_pos_after(sym, pos_after, market_pos)
            if ok:
                tg_send(f"✅ pos_after set {sym}: target {market_pos or ''} {pos_after}")
            else:
                tg_send(f"❌ pos_after fail {sym}: target {market_pos or ''} {pos_after}")
            return ok

        if contracts is not None:
            ok = apply_by_contracts(sym, action, contracts)
            if ok:
                tg_send(f"✅ contracts {sym}: {action} {contracts}")
            else:
                tg_send(f"❌ contracts fail {sym}: {action} {contracts}")
            return ok

        # If no pos_after and no contracts provided, we can infer:
        # - market_position=flat => try to flatten
        if market_pos == "flat":
            long_v, short_v = get_position_volume(sym)
            net = long_v - short_v
            if abs(net) < 1e-9:
                log.info(f"{sym} already flat.")
                return True
            if net > 0:
                # have net long -> sell to flat
                return apply_order(sym, "sell", net)
            else:
                # net short -> buy to flat
                return apply_order(sym, "buy", abs(net))

        # fallback: fixed entry lot (if provided) or symbol min step
        vol = FIXED_ENTRY_LOT
        if vol is None:
            info = get_symbol_info(sym)
            vol = info.volume_min if info and info.volume_min else 0.01
        ok = apply_by_contracts(sym, action, vol)
        return ok

    except Exception as e:
        log.exception(f"handle_signal error: {e}")
        return False

# -----------------------------
# Main loop
# -----------------------------
def main():
    if not mt5_init():
        sys.exit(2)

    acc = get_account_info()
    if acc:
        log.info(f"Account: #{acc.login} {acc.name} leverage={acc.leverage} balance={acc.balance:.2f}")

    tg_send("🤖 MT5 Agent started")

    while True:
        sigs = pull_signals()
        if sigs:
            for sig in sigs:
                ok = handle_signal(sig)
                # ack back if your server expects it (optional)
                try:
                    requests.post(f"{SERVER_URL}/ack", json={
                        "agent_key": AGENT_KEY,
                        "id": sig.get("id"),
                        "ok": bool(ok)
                    }, timeout=5)
                except Exception as e:
                    log.debug(f"/ack failed: {e}")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Agent stopped by user.")
    except Exception as e:
        log.exception(f"Fatal: {e}")
