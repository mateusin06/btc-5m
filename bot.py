#!/usr/bin/env python3
"""
Polymarket BTC 5-Min Up/Down Trading Bot.

Engine principal: timing baseado em relógio, loop de TA, execução de ordens.
Sem log de trades, sem espera de resolução — focado apenas em operar janela por janela.
"""

import argparse
import os
import sys
import time
import threading
import statistics
import math
import requests
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Não sobrescrever variáveis já definidas (ex.: BOT_MODE passado pela web ao iniciar)
if os.path.exists(os.path.join(os.path.dirname(__file__) or ".", ".env")):
    load_dotenv(override=False)


def _parse_markets() -> list[str]:
    v = (os.getenv("BOT_MARKETS") or "").strip().lower()
    if not v:
        return []
    if "," in v:
        out = [m.strip().lower() for m in v.split(",") if m.strip() in ("btc", "eth", "btc15m", "eth15m")]
        return out if out else []
    if v == "both":
        return ["btc", "eth"]
    if v == "eth":
        return ["eth"]
    if v == "btc15m":
        return ["btc15m"]
    if v == "eth15m":
        return ["eth15m"]
    if v == "btc":
        return ["btc"]
    return []

# Configurações de modos (confiança mínima mais assertiva para reduzir entradas ruins de dia)
MODES = {
    "safe": {"min_confidence": 0.72},
    "spike_ai": {"min_confidence": 0.72},  # Igual ao safe, mas com gate de IA (Ollama) antes de executar
    "moon": {"min_confidence": 0.40},  # Estratégia MOON: CVD (divergência + momentum) com mão fixa safe
    "multi_confirm": {"min_confidence": 0.65},  # Multi-Confirmacao + Regime + Divergencia (sinais)
    "aggressive": {"bet_pct": float(os.getenv("AGGRESSIVE_BET_PCT", "25")) / 100.0, "min_confidence": 0.58},
    "degen": {"bet_pct": 1.0, "min_confidence": 0.0},
    "arbitragem": {"min_confidence": 0.30},
    "arb_kalshi": {},
    "only_hedge_plus": {"min_confidence": 0.72},
    "odd_master": {},  # Estratégia própria: últimos 10s, price-to-beat dentro de $10, maior odd
    "90_95": {"min_confidence": 0.55},  # Usa spike e confiança; só entra se preço do lado estiver entre 80c e 95c
}
EV_MIN_MARGIN = 0.02
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.04"))
ARB_POLL_INTERVAL = 1
ARB_DEADLINE_T = 10
ODD_MASTER_LAST_SEC = 10  # Entrar apenas nos últimos 10 segundos
ODD_MASTER_MAX_DIFF_USD = 10.0  # Price to beat entre 0 e $10 de diferença do preço atual
MODE_90_95_LAST_SEC = 20   # 90-95: janela de entrada entre 20s e 2s antes do close
MODE_90_95_EARLY_EXIT = 2  # 90-95: não entrar quando faltar menos de 2s
MODE_90_95_MIN_ODD = 0.80  # 90-95: preço mín. do contrato (80c) — só neste modo
MODE_90_95_MAX_ODD = 0.95  # 90-95: preço máx. do contrato (95c) — só neste modo
ODD_MASTER_EARLY_EXIT = 2  # ODD MASTER: não entrar quando faltar menos de 2s
MOON_MIN_ODD = float(os.getenv("MOON_MIN_ODD", "0.40"))  # MOON: preço mínimo do contrato (ex: 0.40 = 40c)
MOON_DELTA_DIVERGENCE_PCT = 0.03  # MOON: delta mínimo (%) para divergência
MOON_DELTA_MOMENTUM_PCT = 0.07    # MOON: delta mínimo (%) para momentum
MOON_MIN_VOLATILITY_PCT = 0.02    # MOON: volatilidade mínima (%) nos últimos candles
MOON_CVD_NORM_MIN = 0.08          # MOON: CVD normalizado mínimo (|cvd|/vol)
MOON_CVD_MOMENTUM_MIN = 0.03      # MOON: momentum mínimo do CVD normalizado
MOON_CVD_TRADE_LIMIT = 500        # MOON: trades recentes usados para CVD
ARB_KALSHI_MIN_PROFIT_PCT = 0.05  # Arb Kalshi: lucro mínimo (5%)
ARB_KALSHI_SLIPPAGE_PCT = 0.01    # Arb Kalshi: buffer extra para slippage/fees
ARB_KALSHI_POLL_INTERVAL = 2
ARB_KALSHI_PTB_DIFF_BTC = 20.0
ARB_KALSHI_PTB_DIFF_ETH = 1.0
ARB_KALSHI_PTB_WAIT_SEC = 2

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
WINDOW_SEC = 300
WINDOW_SEC_15M = 900
MONITOR_START_T = 90       # BTC/ETH 5m: começar a tentar quando faltar 90s
MONITOR_START_T_15M = 240  # BTC/ETH 15m: começar quando faltar 4 min
HARD_DEADLINE_T = 20       # Parar de tentar quando faltar 40s (safe, aggressive, etc.)
MIN_SECS_TO_ENTER = 20
TA_POLL_INTERVAL = 2
SPIKE_THRESHOLD = 2.2   # Salto mínimo de score para spike (mais assertivo; evita ruído de dia)
SPIKE_MIN_CONFIDENCE = 0.35  # Spike só dispara se confiança >= 35%
T5S_MIN_CONFIDENCE = 0.30   # T-5s só dispara se melhor sinal tiver confiança >= 40%
AI_MIN_CONFIDENCE = float(os.getenv("AI_MIN_CONFIDENCE", "0.60"))
AI_COOLDOWN_SEC = float(os.getenv("AI_COOLDOWN_SEC", "3.0"))
ORDER_RETRY_INTERVAL = 3
ORDER_MAX_FOK_RETRIES = 5  # Limite de retentativas FOK para não bloquear outros mercados (ex: ETH)
MIN_SHARES = 5
POLY_MIN_ORDER_USD = 1.0  # Polymarket exige mínimo $1 por ordem (marketable BUY)
LIMIT_FALLBACK_PRICE = 0.95
MAX_TOKEN_PRICE = float(os.getenv("MAX_TOKEN_PRICE", "0.98"))

# Última janela em que apostamos por mercado (evita repetir no mesmo mercado na mesma janela)
_last_bet_window_by_market: dict[str, int] = {}
_chainlink_ptb_cache: dict[tuple[str, int], float] = {}
_bankroll_lock = threading.Lock()
# Modo fixado no arranque (fonte única de verdade; evita safe quando iniciou em aggressive)
FROZEN_MODE: Optional[str] = None

# (RTDS removido)


@dataclass
class Config:
    dry_run: bool
    mode: str
    once: bool
    max_trades: Optional[int]
    bankroll: float
    min_bet: float
    original_bankroll: float
    fixed_bet_safe: Optional[float] = None
    arbitragem_bet_pct: Optional[float] = None
    kalshi_align_ptb: Optional[bool] = None
    signals_only: bool = False
    fixed_bet_only_hedge: Optional[float] = None
    fixed_bet_odd_master: Optional[float] = None
    fixed_bet_90_95: Optional[float] = None
    markets: list[str] = field(default_factory=lambda: ["btc"])


def delta_to_token_price(delta_pct: float) -> float:
    """Modelo de preço do token baseado no delta (para dry-run/backtest)."""
    abs_d = abs(delta_pct)
    if abs_d < 0.005:
        return 0.50
    if abs_d < 0.02:
        return 0.50 + (abs_d - 0.005) / 0.015 * 0.05
    if abs_d < 0.05:
        return 0.55 + (abs_d - 0.02) / 0.03 * 0.10
    if abs_d < 0.10:
        return 0.65 + (abs_d - 0.05) / 0.05 * 0.15
    if abs_d < 0.15:
        return 0.80 + (abs_d - 0.10) / 0.05 * 0.12
    return min(0.92 + (abs_d - 0.15) * 0.5, 0.97)


def get_window_ts() -> int:
    """Início da janela 5min atual (Unix, múltiplo de 300)."""
    return int(time.time()) // WINDOW_SEC * WINDOW_SEC


def get_window_ts_15m() -> int:
    """Início da janela 15min atual (Unix, múltiplo de 900)."""
    return int(time.time()) // WINDOW_SEC_15M * WINDOW_SEC_15M


def seconds_until_next_window() -> float:
    now = time.time()
    current_start = int(now) // WINDOW_SEC * WINDOW_SEC
    next_start = current_start + WINDOW_SEC
    return (next_start + 1) - now


def seconds_until_next_window_15m() -> float:
    now = time.time()
    current_start = int(now) // WINDOW_SEC_15M * WINDOW_SEC_15M
    next_start = current_start + WINDOW_SEC_15M
    return (next_start + 1) - now


def _get_poly_ptb_chainlink(market: str, window_ts: int) -> Optional[float]:
    """Captura o PTB (abertura) via Chainlink on-chain na virada exata da janela."""
    key = (market, window_ts)
    if key in _chainlink_ptb_cache:
        return _chainlink_ptb_cache[key]
    now = time.time()
    if now < window_ts:
        time.sleep(max(0, window_ts - now))
    # Se passou muito do início da janela, não é mais o PTB exato
    if time.time() - window_ts > ARB_KALSHI_PTB_WAIT_SEC:
        return None
    from api import get_chainlink_latest_price_polygon
    price = get_chainlink_latest_price_polygon(market)
    if price is None:
        return None
    _chainlink_ptb_cache[key] = price
    return price


def seconds_until_close(window_ts: int, window_sec: int) -> int:
    """Segundos até o fechamento da janela (window_ts + window_sec - now)."""
    return (window_ts + window_sec) - int(time.time())


def load_config() -> Config:
    bankroll = float(os.getenv("STARTING_BANKROLL", "10.0"))
    min_bet = float(os.getenv("MIN_BET", "5.0"))
    kalshi_align_ptb = os.getenv("KALSHI_ALIGN_PTB", "0").strip().lower() in ("1", "true", "yes")
    signals_only = os.getenv("SIGNALS_ONLY", "0").strip().lower() in ("1", "true", "yes")
    return Config(
        dry_run=False,
        mode=os.getenv("BOT_MODE", "safe"),
        once=False,
        max_trades=None,
        bankroll=bankroll,
        min_bet=min_bet,
        original_bankroll=bankroll,
        kalshi_align_ptb=kalshi_align_ptb,
        signals_only=signals_only,
        markets=_parse_markets(),
    )


def get_bankroll_from_api(client) -> Optional[float]:
    """Obtém saldo USDC disponível via API CLOB (em USD)."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        resp = client.get_balance_allowance(params)
        if resp and isinstance(resp, dict):
            bal = resp.get("balance")
            if bal is not None:
                raw = float(bal)
                # USDC usa 6 decimais: valor bruto / 1e6 = USD
                if raw > 1000:
                    raw = raw / 1e6
                return raw
        return None
    except Exception:
        return None


def _normalize_private_key(raw: str) -> str:
    """Remove espaços e caracteres não-hex; garante formato aceito por eth_account."""
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip()
    if s.lower() in ("", "0x...", "0x"):
        return ""
    if s.startswith("0x") or s.startswith("0X"):
        prefix = "0x"
        rest = s[2:]
    else:
        prefix = ""
        rest = s
    hex_chars = set("0123456789abcdefABCDEF")
    cleaned = "".join(c for c in rest if c in hex_chars)
    if len(cleaned) != 64:
        raise ValueError(
            f"Chave privada inválida: após limpar caracteres extras restaram {len(cleaned)} caracteres hex (esperado 64). "
            "Verifique se a chave na Config está completa e sem espaços ou caracteres estranhos."
        )
    return prefix + cleaned


def create_clob_client():
    """Cria ClobClient autenticado."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))

    api_key = os.getenv("POLY_API_KEY", "").strip()
    api_secret = os.getenv("POLY_API_SECRET", "").strip()
    api_pass = os.getenv("POLY_API_PASSPHRASE", "").strip()

    if not key or key == "0x...":
        raise ValueError("Defina POLY_PRIVATE_KEY (variável de ambiente ou config na dashboard)")
    try:
        key = _normalize_private_key(key)
    except ValueError as e:
        raise ValueError(str(e))
    if not key:
        raise ValueError("Defina POLY_PRIVATE_KEY (variável de ambiente ou config na dashboard)")
    if not api_key or not api_secret or not api_pass:
        raise ValueError("Defina POLY_API_KEY, POLY_API_SECRET e POLY_API_PASSPHRASE")

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_pass,
    )
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        creds=creds,
        signature_type=sig_type,
        funder=funder or None,
    )
    return client


def _check_ev_plus(
    direction: str,
    result,
    tokens: Optional[list],
    event: Optional[dict],
) -> bool:
    if not tokens or len(tokens) < 2 or not result:
        return False
    from api import get_token_price, get_token_price_from_event

    token_id = tokens[0] if direction == "up" else tokens[1]
    price = get_token_price(token_id, "BUY") or (
        get_token_price_from_event(event, direction) if event else None
    )
    if price is None or price <= 0 or price >= 1:
        return False

    p_up = getattr(result, "estimated_p_up", 0.5)
    volatility_factor = abs((getattr(result, "window_delta_pct", 0) or 0)) / 100.0
    dynamic_margin = 0.03 + (volatility_factor * 0.5)
    dynamic_margin = max(0.03, min(0.08, dynamic_margin))

    direction_upper = direction.upper()
    if direction_upper == "UP":
        return p_up > price + dynamic_margin
    if direction_upper == "DOWN":
        return (1 - p_up) > price + dynamic_margin
    return False


def _dynamic_ev_margin(result) -> float:
    """Margem dinâmica 3%–8% para modo only_hedge_plus (baseada em window_delta_pct)."""
    if result is None:
        return 0.03
    v = abs((getattr(result, "window_delta_pct", 0) or 0)) / 100.0
    return max(0.03, min(0.08, 0.03 + (v * 0.5)))


def _sync_balance_allowance(client) -> None:
    """Atualiza balance/allowance na API CLOB (necessário para proxy/safe)."""
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        client.update_balance_allowance(params=params)
    except Exception as e:
        print(f"  AVISO: update_balance_allowance falhou: {e!s}", flush=True)


def _tg_send(message: str) -> None:
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id or not message:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception as e:
        print(f"  Telegram: falha ao enviar mensagem ({e!s})", flush=True)

def place_fok_order(client, token_id: str, amount_usd: float) -> bool:
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if amount_usd < POLY_MIN_ORDER_USD:
        print(f"  FOK order: valor ${amount_usd:.2f} abaixo do mínimo (${POLY_MIN_ORDER_USD:.2f}), pulando.", flush=True)
        return False
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usd,
        side=BUY,
        price=MAX_TOKEN_PRICE,
        order_type=OrderType.FOK,
    )
    try:
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        return resp.get("status") in ("matched", "live")
    except Exception as e:
        print(f"  FOK order error: {e!s}", flush=True)
        if "Request exception" in str(e):
            raise
        return False


def place_fok_order_at_price(client, token_id: str, amount_usd: float, price_cap: float) -> bool:
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if amount_usd < POLY_MIN_ORDER_USD:
        print(f"  FOK order: valor ${amount_usd:.2f} abaixo do mínimo (${POLY_MIN_ORDER_USD:.2f}), pulando.", flush=True)
        return False
    cap = min(max(0.01, float(price_cap)), MAX_TOKEN_PRICE)
    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usd,
        side=BUY,
        price=cap,
        order_type=OrderType.FOK,
    )
    try:
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        return resp.get("status") in ("matched", "live")
    except Exception as e:
        print(f"  FOK order error: {e!s}", flush=True)
        if "Request exception" in str(e):
            raise
        return False


def place_fok_limit_order(client, token_id: str, size: int, price_cap: float) -> bool:
    """Envia uma ordem LIMIT FOK com quantidade fixa (shares)."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if size <= 0:
        print(f"  FOK limit: size inválido ({size}), pulando.", flush=True)
        return False
    cap = min(max(0.01, float(price_cap)), MAX_TOKEN_PRICE)
    try:
        order = OrderArgs(token_id=token_id, price=cap, size=size, side=BUY)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.FOK)
        return resp.get("status") in ("matched", "live")
    except Exception as e:
        print(f"  FOK limit order error: {e!s}", flush=True)
        if "Request exception" in str(e):
            raise
        return False


def _poly_available_shares(client, token_id: str, price_cap: float) -> tuple[float, Optional[float]]:
    """Retorna (shares_disponiveis, best_ask) até o preço cap."""
    try:
        book = client.get_order_book(token_id)
    except Exception:
        return (0.0, None)
    asks = getattr(book, "asks", None) or []
    total = 0.0
    best_ask = None
    for ask in asks:
        try:
            price = float(getattr(ask, "price", 0) or 0)
            size = float(getattr(ask, "size", 0) or 0)
        except Exception:
            continue
        if best_ask is None or price < best_ask:
            best_ask = price
        if price <= price_cap:
            total += size
    return (total, best_ask)


def _parse_trade_ts(raw) -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        ts = int(raw)
        return ts if ts > 1_000_000_000 else None
    try:
        s = str(raw)
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _poly_has_recent_trade(client, token_id: str, max_age_sec: int = 30) -> bool:
    try:
        from py_clob_client.clob_types import TradeParams
    except Exception:
        return False
    try:
        now = int(time.time())
        trades = client.get_trades(TradeParams(asset_id=token_id))
        for t in trades[:10]:
            ts = _parse_trade_ts(t.get("timestamp") or t.get("created_at") or t.get("time"))
            if ts and (now - ts) <= max_age_sec:
                return True
    except Exception:
        return False
    return False


def place_limit_order(client, token_id: str, amount_usd: float) -> bool:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if amount_usd < POLY_MIN_ORDER_USD:
        print(f"  Limit order: valor ${amount_usd:.2f} abaixo do mínimo (${POLY_MIN_ORDER_USD:.2f}), pulando.", flush=True)
        return False
    limit_price = min(LIMIT_FALLBACK_PRICE, MAX_TOKEN_PRICE)
    shares = amount_usd / limit_price
    if shares < MIN_SHARES:
        shares = MIN_SHARES

    try:
        order = OrderArgs(token_id=token_id, price=limit_price, size=shares, side=BUY)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        return resp.get("status") in ("live", "matched")
    except Exception as e:
        print(f"  Limit order error: {e!s}", flush=True)
        if "Request exception" in str(e):
            raise
        return False


def place_limit_order_exact(client, token_id: str, price: float, shares: float) -> bool:
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    if shares <= 0:
        return False
    limit_price = min(float(price), MAX_TOKEN_PRICE)
    try:
        order = OrderArgs(token_id=token_id, price=limit_price, size=shares, side=BUY)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        return resp.get("status") in ("live", "matched")
    except Exception as e:
        print(f"  Limit order error: {e!s}", flush=True)
        if "Request exception" in str(e):
            raise
        return False


def _kalshi_best_ask(orderbook: dict, side: str) -> tuple[Optional[float], Optional[int]]:
    """Retorna (best_ask_price, max_count) para compra de YES/NO em Kalshi."""
    levels = (orderbook.get("orderbook_fp") or orderbook.get("orderbook") or {})
    yes_bids = levels.get("yes_dollars") or []
    no_bids = levels.get("no_dollars") or []
    # Legacy fallback (centavos): pode vir como "yes"/"no"
    if not yes_bids and not no_bids:
        yes_bids = levels.get("yes") or []
        no_bids = levels.get("no") or []
    if side == "yes":
        if not no_bids:
            return (None, None)
        best_no_bid = max(no_bids, key=lambda x: float(x[0]))
        bid = float(best_no_bid[0])
        if bid > 1.0:
            bid = bid / 100.0
        price = 1.0 - bid
        size = int(float(best_no_bid[1]))
        return (max(price, 0.01), max(size, 0))
    if not yes_bids:
        return (None, None)
    best_yes_bid = max(yes_bids, key=lambda x: float(x[0]))
    bid = float(best_yes_bid[0])
    if bid > 1.0:
        bid = bid / 100.0
    price = 1.0 - bid
    size = int(float(best_yes_bid[1]))
    return (max(price, 0.01), max(size, 0))


def _kalshi_ticker_from_close(market: str, close_ts: int, tz_offset_hours: int = 0, seconds_offset: int = 0) -> str:
    """Monta o ticker Kalshi kxbtc15m-26mar182030 a partir do close_ts."""
    from datetime import datetime, timezone, timedelta
    import calendar

    dt = datetime.fromtimestamp(close_ts, tz=timezone.utc) + timedelta(hours=tz_offset_hours, seconds=seconds_offset)
    day = f"{dt.day:02d}"
    mon = calendar.month_abbr[dt.month].lower()
    hh = f"{dt.hour:02d}"
    mm = f"{dt.minute:02d}"
    ss = f"{dt.second:02d}"
    base = "kxbtc15m" if market.startswith("btc") else "kxeth15m"
    return f"{base}-{day}{mon}{hh}{mm}{ss}"


def _kalshi_candidate_tickers(market: str, close_ts: int) -> list[str]:
    # Tenta UTC e offsets comuns (ET/UTC-3), com variação de segundos (ex.: :30).
    offsets = [0, -3, -4, -5]
    sec_offsets = [0, 30, -30, 60, -60]
    out = []
    seen = set()
    for off in offsets:
        for sec in sec_offsets:
            t = _kalshi_ticker_from_close(market, close_ts, tz_offset_hours=off, seconds_offset=sec)
            if t not in seen:
                seen.add(t)
                out.append(t)
    return out


def _parse_kalshi_close_ts(market_obj: dict) -> Optional[int]:
    from datetime import datetime, timezone
    close_ts = market_obj.get("close_ts")
    if close_ts:
        try:
            return int(close_ts)
        except Exception:
            pass
    close_str = market_obj.get("close_time") or ""
    if close_str:
        try:
            dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            return int(dt.replace(tzinfo=timezone.utc).timestamp())
        except Exception:
            return None
    return None


def _parse_kalshi_price_to_beat(market_obj: dict) -> Optional[float]:
    # Tenta campos conhecidos (Kalshi pode variar por produto)
    candidates = [
        ("floor_strike", "dollars"),
        ("strike_price_dollars", "dollars"),
        ("strike_price_decimal", "dollars"),
        ("strike_price", "dollars"),
        ("price_to_beat", "dollars"),
        ("strike_price_cents", "cents"),
        ("strike_price_in_cents", "cents"),
    ]
    for key, kind in candidates:
        if key in market_obj and market_obj[key] is not None:
            try:
                v = float(market_obj[key])
                return v / 100.0 if kind == "cents" else v
            except Exception:
                pass
    meta = market_obj.get("metadata") or market_obj.get("event_metadata") or {}
    for key, kind in candidates:
        if key in meta and meta[key] is not None:
            try:
                v = float(meta[key])
                return v / 100.0 if kind == "cents" else v
            except Exception:
                pass
    # Tenta extrair de subtítulos (Target Price)
    import re
    for key in ("yes_sub_title", "no_sub_title", "subtitle", "title"):
        s = market_obj.get(key)
        if not s:
            continue
        m = re.search(r"([0-9][0-9,]*\.?[0-9]*)", str(s))
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except Exception:
                pass
    return None


def _find_kalshi_active_market(api_key_id: str, private_key_pem: str, series_ticker: str) -> tuple[Optional[dict], Optional[int]]:
    try:
        from kalshi_api import get_markets
    except Exception:
        return (None, None)
    now_ts = int(time.time())
    cursor = ""
    best = None
    best_close = None
    for _ in range(5):
        data = get_markets(
            api_key_id,
            private_key_pem,
            status="open",
            limit=200,
            cursor=cursor,
            series_ticker=series_ticker,
        )
        markets = data.get("markets") or data.get("data") or []
        for m in markets:
            close_ts = _parse_kalshi_close_ts(m)
            if close_ts is None:
                continue
            if close_ts < now_ts:
                continue
            if best_close is None or close_ts < best_close:
                best = m
                best_close = close_ts
        cursor = data.get("cursor") or data.get("next_cursor") or ""
        if not cursor:
            break
    return (best, best_close)


def _run_kalshi_arb_cycle(config: Config, market: str) -> bool:
    """Arbitragem Polymarket vs Kalshi (BTC/ETH 15m)."""
    from api import (
        get_market_by_slug,
        extract_token_ids,
        get_token_price,
        get_token_price_from_event,
    )
    api_key_id = (os.getenv("KALSHI_API_KEY_ID") or "").strip()
    private_key_pem = (os.getenv("KALSHI_PRIVATE_KEY_PEM") or "").strip()
    if not api_key_id or not private_key_pem:
        print(f"  [{market.upper()}] Arb Kalshi: credenciais Kalshi ausentes.", flush=True)
        return False

    try:
        from kalshi_api import get_balance as kalshi_get_balance, get_orderbook, create_order, get_market as kalshi_get_market
    except Exception as e:
        print(f"  [{market.upper()}] Arb Kalshi: erro ao carregar módulo Kalshi: {e!s}", flush=True)
        return False

    series = "KXBTC15M" if market.startswith("btc") else "KXETH15M"
    kalshi_market, kalshi_close_ts = _find_kalshi_active_market(api_key_id, private_key_pem, series)
    if not kalshi_market or not kalshi_close_ts:
        print(f"  [{market.upper()}] Arb Kalshi: market 15m não encontrado na Kalshi.", flush=True)
        return False
    kalshi_ticker = kalshi_market.get("ticker")
    if not kalshi_ticker:
        print(f"  [{market.upper()}] Arb Kalshi: ticker Kalshi ausente.", flush=True)
        return False

    window_ts = kalshi_close_ts - WINDOW_SEC_15M
    close_time = kalshi_close_ts
    base_market = market.replace("15m", "")
    slug = f"{base_market}-updown-15m-{window_ts}"

    event = get_market_by_slug(slug)
    tokens = extract_token_ids(event) if event else None
    if not tokens or len(tokens) < 2:
        print(f"  [{market.upper()}] Arb Kalshi: mercado Polymarket não encontrado para slug {slug}.", flush=True)
        return False

    if config.kalshi_align_ptb:
        from api import get_chainlink_latest_price_polygon

        now = time.time()
        if now < window_ts:
            time.sleep(max(0, window_ts - now))

        poly_ptb = None
        kalshi_ptb = None
        for attempt in range(1, 6):
            poly_ptb = get_chainlink_latest_price_polygon(base_market)
            kalshi_ptb = _parse_kalshi_price_to_beat(kalshi_market)
            if kalshi_ptb is None:
                try:
                    detail = kalshi_get_market(api_key_id, private_key_pem, kalshi_ticker)
                    kalshi_ptb = _parse_kalshi_price_to_beat(detail.get("market") or detail)
                except Exception:
                    kalshi_ptb = None

            print(
                f"  [{market.upper()}] PTB tentativa {attempt}/5 | Poly {poly_ptb} | Kalshi {kalshi_ptb}",
                flush=True,
            )
            if poly_ptb is not None and kalshi_ptb is not None:
                break
            if attempt < 5:
                time.sleep(10)

        if poly_ptb is None or kalshi_ptb is None:
            print(
                f"  [{market.upper()}] Arb Kalshi: PTB indisponÃ­vel | Poly {poly_ptb} | Kalshi {kalshi_ptb}. Pulando janela.",
                flush=True,
            )
            _last_bet_window_by_market[market] = window_ts
            return False

        diff = abs(float(poly_ptb) - float(kalshi_ptb))
        max_diff = ARB_KALSHI_PTB_DIFF_BTC if market.startswith("btc") else ARB_KALSHI_PTB_DIFF_ETH
        if diff > max_diff:
            print(
                f"  [{market.upper()}] Arb Kalshi: PTB desalinhado | Poly {poly_ptb:.2f} vs Kalshi {kalshi_ptb:.2f} | diff {diff:.2f} > {max_diff:.2f}, pulando janela.",
                flush=True,
            )
            _last_bet_window_by_market[market] = window_ts
            return False

    # Alinhamento Price to Beat (removido)
    if False:
        # Evita checar PTB antes do inÃ­cio da janela (a Gamma costuma preencher sÃ³ depois)
        if time.time() < window_ts:
            return False
        poly_ptb = get_price_to_beat(slug)
        kalshi_ptb = _parse_kalshi_price_to_beat(kalshi_market)
        if kalshi_ptb is None:
            try:
                detail = kalshi_get_market(api_key_id, private_key_pem, kalshi_ticker)
                kalshi_ptb = _parse_kalshi_price_to_beat(detail.get("market") or detail)
            except Exception:
                kalshi_ptb = None

        if poly_ptb is None:
            # Algumas janelas ainda nÃ£o publicam o PTB na Gamma; aguardar um pouco apÃ³s o inÃ­cio
            wait_until = min(close_time - HARD_DEADLINE_T, window_ts + ARB_KALSHI_PTB_WAIT_SEC)
            while poly_ptb is None and time.time() < wait_until:
                time.sleep(1)
                poly_ptb = get_price_to_beat(slug)

        if poly_ptb is None or kalshi_ptb is None:
            print(
                f"  [{market.upper()}] Arb Kalshi: PTB indisponÃ­vel | Poly {poly_ptb} | Kalshi {kalshi_ptb} | slug {slug}. Pulando janela.",
                flush=True,
            )
            return False

        diff = abs(float(poly_ptb) - float(kalshi_ptb))
        max_diff = ARB_KALSHI_PTB_DIFF_BTC if market.startswith("btc") else ARB_KALSHI_PTB_DIFF_ETH
        if diff > max_diff:
            print(
                f"  [{market.upper()}] Arb Kalshi: PTB desalinhado | Poly {poly_ptb:.2f} vs Kalshi {kalshi_ptb:.2f} | diff {diff:.2f} > {max_diff:.2f}, pulando janela.",
                flush=True,
            )
            return False

    print(f"  [{market.upper()}] Arb Kalshi: usando ticker {kalshi_ticker} | slug {slug}", flush=True)

    last_debug = 0.0
    last_liq_log = 0.0
    arb_deadline = HARD_DEADLINE_T
    while int(time.time()) < close_time - arb_deadline:
        # Preços Polymarket
        price_up = get_token_price(tokens[0], "BUY") or (get_token_price_from_event(event, "up") if event else None)
        price_down = get_token_price(tokens[1], "BUY") or (get_token_price_from_event(event, "down") if event else None)
        if price_up is None or price_down is None:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        # Orderbook Kalshi
        try:
            orderbook = get_orderbook(api_key_id, private_key_pem, kalshi_ticker, depth=1)
        except Exception:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        k_yes_ask, k_yes_size = _kalshi_best_ask(orderbook, "yes")
        k_no_ask, k_no_size = _kalshi_best_ask(orderbook, "no")
        if k_yes_ask is None or k_no_ask is None:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        # Cenários de arbitragem (com buffer extra para slippage/fees)
        min_total_cost = 1.0 - (ARB_KALSHI_MIN_PROFIT_PCT + ARB_KALSHI_SLIPPAGE_PCT)
        cand1_cost = float(price_up) + float(k_no_ask)   # YES Poly + NO Kalshi
        cand2_cost = float(price_down) + float(k_yes_ask)  # NO Poly + YES Kalshi
        best = None
        if cand1_cost <= min_total_cost:
            best = ("up", "no", float(price_up), float(k_no_ask), k_no_size)
        if cand2_cost <= min_total_cost:
            if best is None or cand2_cost < cand1_cost:
                best = ("down", "yes", float(price_down), float(k_yes_ask), k_yes_size)
        if not best:
            now = time.time()
            if now - last_debug >= 30:
                last_debug = now
                print(
                    f"  [{market.upper()}] Arb Kalshi: Poly UP {float(price_up):.2f} / DOWN {float(price_down):.2f} | "
                    f"Kalshi YES {k_yes_ask:.2f} / NO {k_no_ask:.2f} | soma1 {cand1_cost:.2f} soma2 {cand2_cost:.2f}",
                    flush=True,
                )
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        trade_direction, kalshi_side, poly_price, kalshi_price, kalshi_size = best
        total_cost = poly_price + kalshi_price
        if total_cost <= 0:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        # Saldos
        api_bankroll = None
        try:
            client_temp = create_clob_client()
            _sync_balance_allowance(client_temp)
            api_bankroll = get_bankroll_from_api(client_temp) or config.bankroll
        except Exception:
            api_bankroll = config.bankroll
        try:
            kalshi_balance = kalshi_get_balance(api_key_id, private_key_pem)
        except Exception:
            kalshi_balance = 0.0

        bankroll = min(api_bankroll or 0, kalshi_balance or 0)
        if not config.arbitragem_bet_pct:
            print(f"  [{market.upper()}] Arb Kalshi: % da banca não definida.", flush=True)
            return False
        bet_budget = bankroll * config.arbitragem_bet_pct
        if bet_budget < POLY_MIN_ORDER_USD:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        contracts = int(bet_budget / total_cost)
        if kalshi_size:
            contracts = min(contracts, int(kalshi_size))
        if contracts < 1:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        min_contracts = int(math.ceil(POLY_MIN_ORDER_USD / max(poly_price, 0.0001)))
        if contracts < min_contracts:
            contracts = min_contracts
            if kalshi_size:
                contracts = min(contracts, int(kalshi_size))

        poly_amount = contracts * poly_price
        kalshi_amount = contracts * kalshi_price
        total_amount = poly_amount + kalshi_amount
        if (
            poly_amount < POLY_MIN_ORDER_USD
            or poly_amount > api_bankroll
            or kalshi_amount > kalshi_balance
            or total_amount > bet_budget
        ):
            now = time.time()
            if now - last_debug >= 30:
                last_debug = now
                print(
                    f"  [{market.upper()}] Arb Kalshi: saldo insuficiente ou abaixo do mínimo | "
                    f"Poly ${api_bankroll:.2f} Kalshi ${kalshi_balance:.2f} | "
                    f"necessário Poly ${poly_amount:.2f} (min ${POLY_MIN_ORDER_USD:.2f}) Kalshi ${kalshi_amount:.2f} | "
                    f"budget ${bet_budget:.2f}",
                    flush=True,
                )
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        print(
            f"  [{market.upper()}] ARB KALSHI: Poly {trade_direction.upper()} @ {poly_price:.2f} + Kalshi {kalshi_side.upper()} @ {kalshi_price:.2f} | N={contracts} | lucro {((1-total_cost)*100):.2f}%",
            flush=True,
        )

        if config.dry_run:
            _last_bet_window_by_market[market] = window_ts
            return True

        # Polymarket primeiro (FOK limit por quantidade), depois Kalshi
        client = create_clob_client()
        _sync_balance_allowance(client)
        token_id = tokens[0] if trade_direction == "up" else tokens[1]
        ok = False
        try:
            # Revalidar preço Polymarket imediatamente antes de enviar
            price_up_now = get_token_price(tokens[0], "BUY") or (get_token_price_from_event(event, "up") if event else None)
            price_down_now = get_token_price(tokens[1], "BUY") or (get_token_price_from_event(event, "down") if event else None)
            if price_up_now is None or price_down_now is None:
                print(f"  [{market.upper()}] Arb Kalshi: preço Polymarket indisponível no envio.", flush=True)
                ok = False
            else:
                poly_now = float(price_up_now) if trade_direction == "up" else float(price_down_now)
                total_now = poly_now + kalshi_price
                min_total_now = 1.0 - (ARB_KALSHI_MIN_PROFIT_PCT + ARB_KALSHI_SLIPPAGE_PCT)
                if total_now > min_total_now:
                    print(f"  [{market.upper()}] Arb Kalshi: oportunidade sumiu (soma {total_now:.2f}).", flush=True)
                    ok = False
                else:
                    # Cap máximo mantendo lucro mínimo + slippage
                    max_poly_price = min_total_now - kalshi_price
                    cap = min(max_poly_price, MAX_TOKEN_PRICE)
                    if cap <= 0:
                        ok = False
                    else:
                        available, best_ask = _poly_available_shares(client, token_id, cap)
                        if available <= 0:
                            now = time.time()
                            if now - last_liq_log >= 30:
                                last_liq_log = now
                                print(
                                    f"  [{market.upper()}] Arb Kalshi: liquidez insuficiente na Poly | "
                                    f"cap {cap:.2f} best {best_ask if best_ask is not None else 'n/a'} | "
                                    f"need {contracts} have {available:.2f}",
                                    flush=True,
                                )
                            ok = False
                        else:
                            # Reduz N para a liquidez real disponível no cap (sem quebrar o mínimo de $1)
                            max_by_liq = int(math.floor(available))
                            new_contracts = min(contracts, max_by_liq)
                            min_contracts_now = int(math.ceil(POLY_MIN_ORDER_USD / max(poly_now, 0.0001)))
                            if new_contracts < min_contracts_now:
                                ok = False
                            else:
                                # Revalidar budget e Kalshi para o novo N
                                poly_amount_now = new_contracts * poly_now
                                kalshi_amount_now = new_contracts * kalshi_price
                                if (
                                    poly_amount_now > api_bankroll
                                    or kalshi_amount_now > kalshi_balance
                                    or (poly_amount_now + kalshi_amount_now) > bet_budget
                                ):
                                    ok = False
                                else:
                                    contracts = new_contracts
                                    ok = place_fok_limit_order(client, token_id, contracts, cap)
            if not ok:
                print(f"  [{market.upper()}] Arb Kalshi: Polymarket FOK falhou, não enviando limit para evitar desbalanceamento.", flush=True)
        except Exception as e:
            print(f"  [{market.upper()}] Arb Kalshi: falha Polymarket {e!s}", flush=True)
            if "Request exception" in str(e):
                # Checa se a ordem chegou a executar antes de tentar novamente
                if _poly_has_recent_trade(client, token_id):
                    print(f"  [{market.upper()}] Arb Kalshi: ordem Poly confirmada após erro de rede.", flush=True)
                    _last_bet_window_by_market[market] = window_ts
                    ok = True
                else:
                    ok = False
            else:
                ok = False

        if not ok:
            time.sleep(ARB_KALSHI_POLL_INTERVAL)
            continue

        _last_bet_window_by_market[market] = window_ts

        # Kalshi (FOK) - após Poly: tentar até o final da janela no mesmo preço ou melhor
        kalshi_ok = False
        last_err = None
        attempt = 0
        while int(time.time()) < close_time - HARD_DEADLINE_T:
            attempt += 1
            try:
                # Revalidar preço Kalshi: só aceita mesmo preço ou melhor (menor)
                try:
                    orderbook_now = get_orderbook(api_key_id, private_key_pem, kalshi_ticker, depth=1)
                    k_yes_now, _ = _kalshi_best_ask(orderbook_now, "yes")
                    k_no_now, _ = _kalshi_best_ask(orderbook_now, "no")
                    k_now = k_yes_now if kalshi_side == "yes" else k_no_now
                except Exception:
                    k_now = None
                if k_now is None or k_now > kalshi_price:
                    print(
                        f"  [{market.upper()}] Arb Kalshi: tentativa {attempt} aguardando preço "
                        f"(atual {k_now if k_now is not None else 'n/a'} > alvo {kalshi_price:.2f})",
                        flush=True,
                    )
                    time.sleep(ARB_KALSHI_POLL_INTERVAL)
                    continue

                print(
                    f"  [{market.upper()}] Arb Kalshi: tentativa {attempt} enviando ordem @ {kalshi_price:.2f}",
                    flush=True,
                )
                kalshi_resp = create_order(
                    api_key_id,
                    private_key_pem,
                    kalshi_ticker,
                    kalshi_side,
                    contracts,
                    kalshi_price,
                    time_in_force="fill_or_kill",
                )
                kalshi_status = kalshi_resp.get("status") or kalshi_resp.get("order", {}).get("status")
                if kalshi_status:
                    print(f"  [{market.upper()}] Arb Kalshi: Kalshi status {kalshi_status}", flush=True)
                kalshi_ok = True
                break
            except Exception as e:
                last_err = e
                print(
                    f"  [{market.upper()}] Arb Kalshi: tentativa {attempt} falhou ({e!s}), tentando novamente...",
                    flush=True,
                )
                time.sleep(ARB_KALSHI_POLL_INTERVAL)

        if not kalshi_ok:
            print(f"  [{market.upper()}] Arb Kalshi: falha Kalshi após Polymarket {last_err!s}", flush=True)
            # Evita repetir Poly na mesma janela quando Kalshi falha depois
            _last_bet_window_by_market[market] = window_ts
            return False

        print(f"  [{market.upper()}] Arb Kalshi executado | N={contracts}", flush=True)
        _tg_send(
            f"[{market.upper()}] ARB_KALSHI: Poly {trade_direction.upper()} @ {poly_price:.2f} + "
            f"Kalshi {kalshi_side.upper()} @ {kalshi_price:.2f} | N={contracts} | lucro {((1-total_cost)*100):.2f}%"
        )
        _last_bet_window_by_market[market] = window_ts
        return True

    return False


def _run_multi_confirm_signal(config: Config, market: str, window_ts: int, window_sec: int, slug: str) -> bool:
    from api import get_price_by_market, get_candles_by_market, get_btc_candles_1m
    from strategy import analyze_multi_confirm, MIN_CANDLES_FOR_FULL_TA

    is_15m = market.endswith("15m")
    close_time = window_ts + window_sec
    monitor_secs = MONITOR_START_T_15M if is_15m else MONITOR_START_T

    while True:
        secs = seconds_until_close(window_ts, window_sec)
        if secs <= monitor_secs:
            break
        if secs % 60 == 0 or secs <= 30:
            print(f"  [{market.upper()}] Janela fecha em {secs}s... (slug: {slug})", flush=True)
        time.sleep(1)

    candle_limit = 15 if not is_15m else 5
    candles = get_candles_by_market(market, limit=candle_limit)
    window_open = None
    for c in candles:
        candle_start_sec = c["t"] // 1000
        if candle_start_sec == window_ts:
            window_open = c["o"]
            break
    if window_open is None:
        if candles:
            window_open = candles[-1]["o"] if is_15m else candles[0]["o"]
        else:
            window_open = get_price_by_market(market) or 0

    if not window_open:
        print(f"  [{market.upper()}] MC+RD: nao foi possivel obter preco de abertura.", flush=True)
        _last_bet_window_by_market[market] = window_ts
        return False

    tick_prices: list[float] = []
    while int(time.time()) < close_time - HARD_DEADLINE_T:
        price = get_price_by_market(market)
        if price:
            tick_prices.append(price)
        candle_limit_ta = max(30, MIN_CANDLES_FOR_FULL_TA)
        candles = get_btc_candles_1m(limit=candle_limit_ta) if is_15m else get_candles_by_market(market, limit=candle_limit_ta)
        if not candles or len(candles) < MIN_CANDLES_FOR_FULL_TA:
            time.sleep(TA_POLL_INTERVAL)
            continue
        current_price = price or candles[-1]["c"]
        signal = analyze_multi_confirm(window_open, current_price, candles, tick_prices[-30:] if tick_prices else None)
        if signal is not None:
            log = (
                f"  [{market.upper()}] MC+RD: {signal.direction.upper()} | "
                f"conf {signal.confidence:.0%} | {signal.reason}"
            )
            print(log, flush=True)
            market_link = f"https://polymarket.com/market/{slug}"
            _tg_send(
                f"[{market.upper()}] SINAL MC+RD: {signal.direction.upper()} | "
                f"conf {signal.confidence:.0%} | {signal.reason}\n{market_link}"
            )
            _last_bet_window_by_market[market] = window_ts
            return True
        time.sleep(TA_POLL_INTERVAL)

    print(f"  [{market.upper()}] MC+RD: sem sinal valido, pulando janela.", flush=True)
    _last_bet_window_by_market[market] = window_ts
    return False


def run_trade_cycle(config: Config, market: str, active_mode: Optional[str] = None, shared: Optional[dict] = None) -> bool:
    """Executa um ciclo para um mercado (btc, eth ou btc15m): espera, analisa, opera.
    active_mode: modo fixado no arranque (evita mistura safe/aggressive); se None, usa FROZEN_MODE ou config.mode.
    """
    global _last_bet_window_by_market
    from api import get_market_by_slug, extract_token_ids, get_price_by_market, get_candles_by_market, get_btc_candles_1m
    from strategy import analyze, MIN_CANDLES_FOR_FULL_TA

    # Fonte única de verdade: SEMPRE usar FROZEN_MODE quando estiver definido (evita 2 compras com modos diferentes)
    if FROZEN_MODE is not None:
        active_mode = FROZEN_MODE
    elif active_mode is None:
        active_mode = config.mode
    is_15m = market.endswith("15m")
    base_market = market.replace("15m", "") if is_15m else market
    window_sec = WINDOW_SEC_15M if is_15m else WINDOW_SEC
    window_ts = get_window_ts_15m() if is_15m else get_window_ts()
    close_time = window_ts + window_sec
    if active_mode == "arb_kalshi" and not is_15m:
        print(f"  [{market.upper()}] Arb Kalshi: mercado inválido (use BTC15m/ETH15m).", flush=True)
        return False
    # Safe/aggressive: BTC-ETH 5m = desde 2 min até 40s; BTC 15m = desde 5 min até 40s
    monitor_secs = (
        MONITOR_START_T_15M if is_15m else (
            ODD_MASTER_LAST_SEC if active_mode == "odd_master" else
            (WINDOW_SEC if active_mode == "arbitragem" else MONITOR_START_T)
        )
    )

    # Se já passou do prazo para operar nesta janela (ex.: thread reentrou logo após ciclo), usar próxima janela
    secs_left = seconds_until_close(window_ts, window_sec)
    if secs_left < HARD_DEADLINE_T:
        window_ts = window_ts + window_sec
        close_time = window_ts + window_sec

    slug = f"{base_market}-updown-15m-{window_ts}" if is_15m else f"{market}-updown-5m-{window_ts}"

    # Não apostar mais de uma vez na mesma janela por mercado (1 compra por mercado por janela)
    if _last_bet_window_by_market.get(market) == window_ts:
        return False

    if active_mode == "arb_kalshi":
        return _run_kalshi_arb_cycle(config, market)

    while True:
        secs = seconds_until_close(window_ts, window_sec)
        if secs <= monitor_secs:
            break
        if secs % 60 == 0 or secs <= 30:
            print(f"  [{market.upper()}] Janela fecha em {secs}s... (slug: {slug})", flush=True)
        time.sleep(1)

    candle_limit = 15 if not is_15m else 5
    candles = get_candles_by_market(market, limit=candle_limit)
    window_open = None
    for c in candles:
        candle_start_sec = c["t"] // 1000
        if candle_start_sec == window_ts:
            window_open = c["o"]
            break
    if window_open is None:
        if candles:
            window_open = candles[-1]["o"] if is_15m else candles[0]["o"]
        else:
            window_open = get_price_by_market(market) or 0

    if not window_open:
        print(f"  [{market.upper()}] ERRO: Não foi possível obter preço de abertura.", flush=True)
        return False

    event = get_market_by_slug(slug)
    tokens = extract_token_ids(event) if event else None


    tick_prices = []
    best_score = 0.0
    best_result = None
    prev_score = 0.0
    fired = False
    trade_direction = None
    final_result = None
    ai_denied_until = 0.0

    # Para ODD MASTER e 90-95: loop até 2s antes do close; safe/aggressive/etc.: até 40s antes (desde 2min ou 5min conforme mercado)
    if active_mode in ("odd_master", "90_95"):
        deadline_sec = ODD_MASTER_EARLY_EXIT  # 2s — assim o loop continua até faltar 2s; dentro do bloco checamos 2 <= secs <= 10
    else:
        deadline_sec = HARD_DEADLINE_T

    while int(time.time()) < close_time - deadline_sec:
        price = get_price_by_market(market)
        # Modo ODD MASTER: entrar SOMENTE entre 10s e 2s antes do close; lado de MAIOR ODD = menor preço (centavos)
        if active_mode == "odd_master" and tokens and len(tokens) == 2 and event:
            secs = seconds_until_close(window_ts, window_sec)
            if secs > ODD_MASTER_LAST_SEC or secs < ODD_MASTER_EARLY_EXIT:
                time.sleep(1)
                continue
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev
            current_price = get_price_by_market(market)
            if current_price is not None and window_open is not None:
                diff_usd = abs(float(current_price) - float(window_open))
                if 0 <= diff_usd <= ODD_MASTER_MAX_DIFF_USD:
                    price_up = _get_tok(tokens[0], "BUY") or _get_ev(event, "up")
                    price_down = _get_tok(tokens[1], "BUY") or _get_ev(event, "down")
                    if price_up is not None and price_down is not None and 0 < price_up < 1 and 0 < price_down < 1:
                        # Maior odd = menor preço em centavos (underdog)
                        if price_up <= price_down:
                            trade_direction = "up"
                            odd = price_up
                        else:
                            trade_direction = "down"
                            odd = price_down
                        fired = True
                        print(f"  [{market.upper()}] ODD MASTER: price-to-beat ${window_open:.2f} vs atual ${current_price:.2f} (diff ${diff_usd:.2f}) | maior odd (menor preço) = {trade_direction.upper()} @ {odd:.2f}", flush=True)
                        break
            time.sleep(1)
            continue

        # Modo 90-95: janela 20s–2s; usa spike e confiança, mas só dispara se o preço do lado escolhido estiver entre 80c e 95c
        if active_mode == "90_95" and tokens and len(tokens) == 2 and event:
            secs = seconds_until_close(window_ts, window_sec)
            # Só analisar e tentar entrar quando 2 <= secs <= 20 (desde 20s até 2s antes do close)
            if secs > MODE_90_95_LAST_SEC:
                time.sleep(1)
                continue
            if secs < MODE_90_95_EARLY_EXIT:
                time.sleep(1)
                continue
            # Dentro da janela: segue para TA (analyze, spike, confiança)
            print(f"  [{market.upper()}] 90-95: janela ativa ({secs}s para close), analisando...", flush=True)

        # Modo MOON: estratégia baseada em CVD (divergência + momentum)
        if active_mode == "moon":
            from api import get_cvd_snapshot

            candle_limit_ta = max(30, MIN_CANDLES_FOR_FULL_TA)
            candles = get_btc_candles_1m(limit=candle_limit_ta) if is_15m else get_candles_by_market(market, limit=candle_limit_ta)
            if not candles or len(candles) < MIN_CANDLES_FOR_FULL_TA:
                time.sleep(1)
                continue

            current_price = price or (candles[-1]["c"] if candles else None)
            if window_open and current_price:
                delta_pct = (float(current_price) - float(window_open)) / float(window_open) * 100.0
                snap = get_cvd_snapshot(market, limit=MOON_CVD_TRADE_LIMIT)
                total_vol = snap.get("total_vol") or 0.0
                cvd = snap.get("cvd") or 0.0
                cvd_norm = (cvd / total_vol) if total_vol > 0 else 0.0
                vol_recent = snap.get("vol_recent") or 0.0
                vol_prev = snap.get("vol_prev") or 0.0
                cvd_recent_norm = (snap.get("cvd_recent") or 0.0) / vol_recent if vol_recent > 0 else 0.0
                cvd_prev_norm = (snap.get("cvd_prev") or 0.0) / vol_prev if vol_prev > 0 else 0.0
                cvd_momentum = cvd_recent_norm - cvd_prev_norm

                # Volatilidade mínima (evita entrar em mercado travado)
                prices = [c["c"] for c in candles[-10:]]
                returns = []
                for i in range(1, len(prices)):
                    if prices[i - 1] != 0:
                        returns.append((prices[i] - prices[i - 1]) / prices[i - 1])
                vol_pct = statistics.pstdev(returns) * 100 if len(returns) >= 5 else 0.0
                if vol_pct < MOON_MIN_VOLATILITY_PCT:
                    time.sleep(1)
                    continue

                # Confirmação de micro-preço: candle 1m atual na direção do sinal
                last_o = candles[-1]["o"]
                last_c = candles[-1]["c"]
                micro_dir = 1 if last_c > last_o else (-1 if last_c < last_o else 0)
                signal = None
                reason = ""
                # Divergência bullish: preço caiu >= -0.02% e CVD > +10
                if (
                    delta_pct <= -MOON_DELTA_DIVERGENCE_PCT
                    and cvd_norm >= MOON_CVD_NORM_MIN
                    and cvd_momentum >= MOON_CVD_MOMENTUM_MIN
                    and micro_dir > 0
                ):
                    signal = "up"
                    reason = f"Divergência bullish: preço {delta_pct:.3f}% | CVDn {cvd_norm:.2f} | mom {cvd_momentum:.2f}"
                # Divergência bearish: preço subiu >= +0.02% e CVD < -10
                elif (
                    delta_pct >= MOON_DELTA_DIVERGENCE_PCT
                    and cvd_norm <= -MOON_CVD_NORM_MIN
                    and cvd_momentum <= -MOON_CVD_MOMENTUM_MIN
                    and micro_dir < 0
                ):
                    signal = "down"
                    reason = f"Divergência bearish: preço {delta_pct:.3f}% | CVDn {cvd_norm:.2f} | mom {cvd_momentum:.2f}"
                # Momentum bullish: preço subiu > +0.05% e CVD > +20
                elif (
                    delta_pct > MOON_DELTA_MOMENTUM_PCT
                    and cvd_norm >= MOON_CVD_NORM_MIN
                    and cvd_momentum >= MOON_CVD_MOMENTUM_MIN
                    and micro_dir > 0
                ):
                    signal = "up"
                    reason = f"Momentum bullish: preço {delta_pct:.3f}% | CVDn {cvd_norm:.2f} | mom {cvd_momentum:.2f}"
                # Momentum bearish: preço caiu < -0.05% e CVD < -20
                elif (
                    delta_pct < -MOON_DELTA_MOMENTUM_PCT
                    and cvd_norm <= -MOON_CVD_NORM_MIN
                    and cvd_momentum <= -MOON_CVD_MOMENTUM_MIN
                    and micro_dir < 0
                ):
                    signal = "down"
                    reason = f"Momentum bearish: preço {delta_pct:.3f}% | CVDn {cvd_norm:.2f} | mom {cvd_momentum:.2f}"

                if signal is not None:
                    mode_cfg = MODES.get(active_mode, MODES["safe"])
                    min_conf = mode_cfg.get("min_confidence")
                    if min_conf is not None:
                        candle_limit_ta = max(30, MIN_CANDLES_FOR_FULL_TA)
                        candles = get_btc_candles_1m(limit=candle_limit_ta) if is_15m else get_candles_by_market(market, limit=candle_limit_ta)
                        if not candles or len(candles) < MIN_CANDLES_FOR_FULL_TA:
                            print(f"  [{market.upper()}] MOON: candles insuficientes para confiança, pulando sinal.", flush=True)
                            time.sleep(1)
                            continue
                        moon_result = analyze(
                            window_open,
                            float(current_price),
                            candles,
                            tick_prices[-20:] if tick_prices else None,
                        )
                        if moon_result.confidence < float(min_conf):
                            print(f"  [{market.upper()}] MOON: sinal {signal.upper()} ignorado (confiança {moon_result.confidence:.0%} < {min_conf:.0%})", flush=True)
                            time.sleep(1)
                            continue
                    trade_direction = signal
                    final_result = None
                    fired = True
                    print(f"  [{market.upper()}] MOON: {signal.upper()} | {reason}", flush=True)
                    break

            time.sleep(TA_POLL_INTERVAL)
            continue

        # Modo Multi-Confirmacao + Regime + Divergencia
        if active_mode == "multi_confirm":
            from strategy import analyze_multi_confirm

            candle_limit_ta = max(30, MIN_CANDLES_FOR_FULL_TA)
            candles = get_btc_candles_1m(limit=candle_limit_ta) if is_15m else get_candles_by_market(market, limit=candle_limit_ta)
            if not candles or len(candles) < MIN_CANDLES_FOR_FULL_TA:
                time.sleep(TA_POLL_INTERVAL)
                continue
            current_price = price or candles[-1]["c"]
            signal = analyze_multi_confirm(
                window_open,
                float(current_price),
                candles,
                tick_prices[-30:] if tick_prices else None,
            )
            if signal is None:
                time.sleep(TA_POLL_INTERVAL)
                continue
            trade_direction = signal.direction
            fired = True
            final_result = None
            print(
                f"  [{market.upper()}] MC+RD: {signal.direction.upper()} | conf {signal.confidence:.0%} | {signal.reason}",
                flush=True,
            )
            _tg_send(
                f"[{market.upper()}] MC+RD: {signal.direction.upper()} | conf {signal.confidence:.0%} | {signal.reason}\n"
                f"https://polymarket.com/market/{slug}"
            )
            break

        # Modo arbitragem: prioridade para arb pura a cada iteração (Up+Down < 1-margem)
        if active_mode == "arbitragem" and tokens and len(tokens) == 2 and event:
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev
            price_up = _get_tok(tokens[0], "BUY") or _get_ev(event, "up")
            price_down = _get_tok(tokens[1], "BUY") or _get_ev(event, "down")
            if (price_up is not None and price_down is not None
                    and 0 < price_up < 1 and 0 < price_down < 1
                    and (price_up + price_down) <= (1.0 - ARB_MIN_PROFIT_PCT)):
                fired = True
                trade_direction = "arb_pura"
                print(f"  [{market.upper()}] ARB PURA detectada: Up @ {price_up:.2f} + Down @ {price_down:.2f} = {price_up+price_down:.2f} (lucro garantido)", flush=True)
                break

        if price:
            tick_prices.append(price)
        # Candles 1m com volume: pelo menos MIN_CANDLES_FOR_FULL_TA para RSI, Volume Surge, Acceleration, Micro Momentum, EMA
        candle_limit_ta = max(30, MIN_CANDLES_FOR_FULL_TA)
        candles = get_btc_candles_1m(limit=candle_limit_ta) if is_15m else get_candles_by_market(market, limit=candle_limit_ta)
        if not candles or len(candles) < MIN_CANDLES_FOR_FULL_TA:
            time.sleep(ARB_POLL_INTERVAL if active_mode == "arbitragem" else (1 if active_mode == "90_95" else TA_POLL_INTERVAL))
            continue

        result = analyze(window_open, price or candles[-1]["c"], candles, tick_prices[-20:] if tick_prices else None)

        if abs(result.score) > abs(best_score):
            best_score = result.score
            best_result = result

        # Modo 90-95: sem estratégia (spike/confiança/T-5s); só filtros: janela 20s–2s + preço 80–95c
        if active_mode == "90_95" and tokens and len(tokens) == 2 and event:
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev
            trade_direction = result.direction
            final_result = result
            tok_price = (_get_tok(tokens[0], "BUY") or _get_ev(event, "up")) if trade_direction == "up" else (_get_tok(tokens[1], "BUY") or _get_ev(event, "down"))
            if tok_price is not None and MODE_90_95_MIN_ODD <= tok_price <= MODE_90_95_MAX_ODD:
                fired = True
                print(f"  [{market.upper()}] 90-95: {trade_direction.upper()} @ {tok_price:.2f} (80–95c) -> executando ordem", flush=True)
                break

        if active_mode == "90_95":
            prev_score = result.score
            poll = ARB_POLL_INTERVAL if active_mode == "arbitragem" else (1 if active_mode == "90_95" else TA_POLL_INTERVAL)
            time.sleep(poll)
            continue

        if active_mode != "moon" and abs(result.score - prev_score) >= SPIKE_THRESHOLD and prev_score != 0:
            # Spike mais assertivo: só dispara se confiança mínima (evita ruído)
            if result.confidence >= SPIKE_MIN_CONFIDENCE:
                trade_direction = result.direction
                final_result = result
                if active_mode == "only_hedge_plus":
                    if _check_ev_plus(trade_direction, final_result, tokens, event):
                        fired = True
                        print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction} (EV+)", flush=True)
                        break
                    else:
                        print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction} (sem EV+, continuando)", flush=True)
                else:
                    if active_mode == "spike_ai":
                        if time.time() < ai_denied_until:
                            pass
                        else:
                            from ai import ask_ollama_trade_gate
                            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev

                            secs_to_close = seconds_until_close(window_ts, window_sec)
                            tok_price = None
                            if tokens and len(tokens) == 2 and event:
                                tok_price = (_get_tok(tokens[0], "BUY") or _get_ev(event, "up")) if trade_direction == "up" else (_get_tok(tokens[1], "BUY") or _get_ev(event, "down"))

                            decision = ask_ollama_trade_gate(
                                market=market,
                                side=trade_direction,
                                seconds_to_close=secs_to_close,
                                token_price=tok_price,
                                ta_details=getattr(result, "details", {}) or {},
                                score=result.score,
                                confidence=result.confidence,
                                window_open=window_open,
                                current_price=(price or candles[-1]["c"]),
                                mode=active_mode,
                            )
                            if decision.allow and decision.confidence >= AI_MIN_CONFIDENCE:
                                fired = True
                                print(f"  [{market.upper()}] SPIKE AI: aprovado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
                                break
                            ai_denied_until = time.time() + AI_COOLDOWN_SEC
                            print(f"  [{market.upper()}] SPIKE AI: negado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
                    else:
                        fired = True
                        print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction}", flush=True)
                        break
            else:
                print(f"  [{market.upper()}] SPIKE ignorado (confiança {result.confidence:.1%} < {SPIKE_MIN_CONFIDENCE:.0%})", flush=True)

        mode_cfg = MODES.get(active_mode, MODES["safe"])
        if result.confidence >= mode_cfg.get("min_confidence", 0):
            trade_direction = result.direction
            final_result = result
            if active_mode == "only_hedge_plus":
                if _check_ev_plus(trade_direction, final_result, tokens, event):
                    fired = True
                    print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction} (EV+)", flush=True)
                    break
                else:
                    print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction} (sem EV+, continuando)", flush=True)
            else:
                if active_mode == "spike_ai":
                    if time.time() < ai_denied_until:
                        pass
                    else:
                        from ai import ask_ollama_trade_gate
                        from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev

                        secs_to_close = seconds_until_close(window_ts, window_sec)
                        tok_price = None
                        if tokens and len(tokens) == 2 and event:
                            tok_price = (_get_tok(tokens[0], "BUY") or _get_ev(event, "up")) if trade_direction == "up" else (_get_tok(tokens[1], "BUY") or _get_ev(event, "down"))

                        decision = ask_ollama_trade_gate(
                            market=market,
                            side=trade_direction,
                            seconds_to_close=secs_to_close,
                            token_price=tok_price,
                            ta_details=getattr(result, "details", {}) or {},
                            score=result.score,
                            confidence=result.confidence,
                            window_open=window_open,
                            current_price=(price or candles[-1]["c"]),
                            mode=active_mode,
                        )
                        if decision.allow and decision.confidence >= AI_MIN_CONFIDENCE:
                            fired = True
                            print(f"  [{market.upper()}] SPIKE AI: aprovado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
                            break
                        ai_denied_until = time.time() + AI_COOLDOWN_SEC
                        print(f"  [{market.upper()}] SPIKE AI: negado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
                else:
                    fired = True
                    print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction}", flush=True)
                    break

        prev_score = result.score
        poll = ARB_POLL_INTERVAL if active_mode == "arbitragem" else (1 if active_mode == "90_95" else TA_POLL_INTERVAL)
        time.sleep(poll)

    if not fired and best_result and active_mode != "moon":
        trade_direction = best_result.direction
        final_result = best_result
        # T-5s mais assertivo: só entra se o melhor sinal tiver confiança mínima
        if best_result.confidence < T5S_MIN_CONFIDENCE:
            print(f"  [{market.upper()}] T-5s: melhor sinal com confiança {best_result.confidence:.1%} < {T5S_MIN_CONFIDENCE:.0%}, pulando.", flush=True)
        elif active_mode == "only_hedge_plus":
            if _check_ev_plus(trade_direction, final_result, tokens, event):
                fired = True
                print(f"  [{market.upper()}] T-5s: melhor sinal -> {trade_direction} (score {best_score:.2f}) [EV+]", flush=True)
            else:
                dm = _dynamic_ev_margin(final_result)
                print(f"  [{market.upper()}] T-5s: sinal sem EV+ (P não > preço+margem {dm:.0%}), pulando.", flush=True)
        elif active_mode == "90_95":
            # 90-95 não usa T-5s (apenas filtros janela + 80–95c)
            pass
        elif active_mode == "spike_ai":
            from ai import ask_ollama_trade_gate
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev

            secs_to_close = seconds_until_close(window_ts, window_sec)
            tok_price = None
            if tokens and len(tokens) == 2 and event:
                tok_price = (_get_tok(tokens[0], "BUY") or _get_ev(event, "up")) if trade_direction == "up" else (_get_tok(tokens[1], "BUY") or _get_ev(event, "down"))

            decision = ask_ollama_trade_gate(
                market=market,
                side=trade_direction,
                seconds_to_close=secs_to_close,
                token_price=tok_price,
                ta_details=getattr(best_result, "details", {}) or {},
                score=best_score,
                confidence=best_result.confidence,
                window_open=window_open,
                current_price=(price or candles[-1]["c"]),
                mode=active_mode,
            )
            if decision.allow and decision.confidence >= AI_MIN_CONFIDENCE:
                fired = True
                print(f"  [{market.upper()}] T-5s SPIKE AI: aprovado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
            else:
                print(f"  [{market.upper()}] T-5s SPIKE AI: negado ({decision.confidence:.0%}) -> {trade_direction.upper()} | {decision.reason}", flush=True)
        else:
            fired = True
            print(f"  [{market.upper()}] T-5s: melhor sinal -> {trade_direction} (score {best_score:.2f})", flush=True)

    if not fired or not trade_direction:
        print(f"  [{market.upper()}] Sem sinal válido, pulando janela.", flush=True)
        _last_bet_window_by_market[market] = window_ts
        return False

    if config.signals_only:
        print(f"  [{market.upper()}] Sinal confirmado ({trade_direction.upper()}) | apenas sinais, sem ordem.", flush=True)
        _tg_send(
            f"[{market.upper()}] SINAL: {trade_direction.upper()} | apenas sinais (sem ordem)\n"
            f"https://polymarket.com/market/{slug}"
        )
        _last_bet_window_by_market[market] = window_ts
        return True

    if active_mode == "90_95":
        print(f"  [{market.upper()}] 90-95: sinal válido -> {trade_direction.upper()}, calculando aposta e executando.", flush=True)

    # 4. Calcular tamanho da aposta
    # Safe e only_hedge_plus: valor fixo
    # Aggressive e arbitragem: % da banca via API (real) ou config.bankroll (dry run)
    api_bankroll = None
    if not config.dry_run:
        client_temp = create_clob_client()
        _sync_balance_allowance(client_temp)
        api_bankroll = get_bankroll_from_api(client_temp) or config.bankroll

        # Stop Win / Stop Loss: comparar bankroll atual (API) com o inicial (config)
        try:
            stop_win_enabled = (os.getenv("STOP_WIN_ENABLED") or "").strip() in ("1", "true", "yes")
            stop_loss_enabled = (os.getenv("STOP_LOSS_ENABLED") or "").strip() in ("1", "true", "yes")
            initial_str = os.getenv("STOP_WIN_LOSS_INITIAL_BANKROLL") or ""
            initial = float(initial_str) if initial_str else (getattr(config, "original_bankroll", None) or config.bankroll)
            if (stop_win_enabled or stop_loss_enabled) and api_bankroll is not None and initial > 0:
                if stop_win_enabled:
                    pct = float(os.getenv("STOP_WIN_PCT", "0") or "0")
                    if pct > 0 and api_bankroll >= initial * (1 + pct / 100):
                        print(f"  [STOP WIN] Bankroll ${api_bankroll:.2f} >= inicial ${initial:.2f} + {pct:.1f}% (${initial * (1 + pct/100):.2f}). Parando.", flush=True)
                        if shared is not None:
                            shared["stop"] = True
                        return False
                if stop_loss_enabled:
                    pct = float(os.getenv("STOP_LOSS_PCT", "0") or "0")
                    if pct > 0 and api_bankroll <= initial * (1 - pct / 100):
                        print(f"  [STOP LOSS] Bankroll ${api_bankroll:.2f} <= inicial ${initial:.2f} - {pct:.1f}% (${initial * (1 - pct/100):.2f}). Parando.", flush=True)
                        if shared is not None:
                            shared["stop"] = True
                        return False
        except (ValueError, TypeError):
            pass

    if active_mode in ("safe", "spike_ai", "moon", "multi_confirm") and config.fixed_bet_safe is not None:
        bet_size = config.fixed_bet_safe
    elif active_mode == "only_hedge_plus":
        bet_size = config.fixed_bet_only_hedge if config.fixed_bet_only_hedge is not None else config.min_bet
    elif active_mode == "odd_master":
        bet_size = config.fixed_bet_odd_master if config.fixed_bet_odd_master is not None else config.min_bet
    elif active_mode == "90_95":
        bet_size = config.fixed_bet_90_95 if config.fixed_bet_90_95 is not None else config.min_bet
    elif active_mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        bankroll = api_bankroll if api_bankroll is not None else config.bankroll
        bet_size = bankroll * config.arbitragem_bet_pct
    elif active_mode == "aggressive":
        mode_cfg = MODES.get(active_mode, MODES["safe"])
        bankroll = api_bankroll if api_bankroll is not None else config.bankroll
        bet_size = bankroll * mode_cfg.get("bet_pct", 0.25)
    else:
        bet_size = (api_bankroll or config.bankroll) * MODES.get(active_mode, MODES["safe"]).get("bet_pct", 0.25)

    bet_size = max(bet_size, config.min_bet)
    bet_size = max(bet_size, POLY_MIN_ORDER_USD)  # Polymarket exige mínimo $1 por ordem
    cap = api_bankroll if api_bankroll is not None else config.bankroll
    if bet_size > cap:
        bet_size = cap

    if active_mode in ("aggressive", "arbitragem") and api_bankroll is not None:
        pct = (config.arbitragem_bet_pct * 100) if active_mode == "arbitragem" else (MODES.get(active_mode, MODES["safe"]).get("bet_pct", 0.25) * 100)
        print(f"  [{market.upper()}] Saldo API: ${api_bankroll:.2f} | aposta {pct:.0f}% = ${bet_size:.2f}", flush=True)

    if bet_size < config.min_bet:
        print(f"  [{market.upper()}] Bankroll insuficiente: ${cap:.2f} < min ${config.min_bet:.2f}", flush=True)
        return False

    # 5. Dry run
    if config.dry_run:
        from api import get_token_price, get_token_price_from_event
        if active_mode == "arbitragem" and (trade_direction == "arb_pura" or (tokens and len(tokens) == 2 and event)):
            price_up = get_token_price(tokens[0], "BUY") or get_token_price_from_event(event, "up")
            price_down = get_token_price(tokens[1], "BUY") or get_token_price_from_event(event, "down")
            if price_up is not None and price_down is not None and 0 < price_up < 1 and 0 < price_down < 1:
                total_price = price_up + price_down
                if total_price <= (1.0 - ARB_MIN_PROFIT_PCT):
                    shares_arb = bet_size / total_price
                    bet_pct_arb = (bet_size / config.bankroll) * 100 if config.bankroll else 0
                    print(f"  [{market.upper()}] DRY RUN ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} | {shares_arb:.2f} shares | aposta: {bet_pct_arb:.1f}% da banca", flush=True)
                    _last_bet_window_by_market[market] = window_ts
                    return True
                if trade_direction == "arb_pura":
                    print(f"  [{market.upper()}] DRY RUN: arb sumiu (soma agora {total_price:.2f}), pulando.", flush=True)
                    return False
            if trade_direction == "arb_pura":
                print(f"  [{market.upper()}] DRY RUN: arb sumiu (preços indisponíveis), pulando.", flush=True)
                return False
        if trade_direction in ("up", "down"):
            token_id_dry = tokens[0] if trade_direction == "up" else tokens[1] if tokens else None
            token_price = None
            if token_id_dry:
                token_price = get_token_price(token_id_dry, "BUY")
                if token_price is None and int(time.time()) < close_time:
                    for _ in range(5):
                        time.sleep(2)
                        token_price = get_token_price(token_id_dry, "BUY")
                        if token_price is not None:
                            break
            if token_price is None and event:
                token_price = get_token_price_from_event(event, trade_direction)
            if token_price is None:
                token_price = delta_to_token_price(final_result.window_delta_pct if final_result else 0)
            if token_price > MAX_TOKEN_PRICE:
                if active_mode != "arbitragem":
                    print(f"  [{market.upper()}] Token @ ${token_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
                    return False
            if active_mode == "moon" and token_price is not None and token_price < MOON_MIN_ODD:
                print(f"  [{market.upper()}] MOON: Token @ ${token_price:.2f} < ${MOON_MIN_ODD:.2f}, pulando.", flush=True)
                return False
            # Modo 90-95: só executar (mesmo dry run) se preço ainda estiver entre 80c e 95c
            if active_mode == "90_95" and token_price is not None:
                if token_price < MODE_90_95_MIN_ODD or token_price > MODE_90_95_MAX_ODD:
                    print(f"  [{market.upper()}] DRY RUN 90-95: preço atual ${token_price:.2f} fora da faixa 80–95c, pulando.", flush=True)
                    return False
            shares = bet_size / token_price
            if active_mode == "arbitragem" and tokens and len(tokens) == 2:
                token_other_id = tokens[1] if trade_direction == "up" else tokens[0]
                max_other_price = (1.0 - ARB_MIN_PROFIT_PCT) - token_price
                if max_other_price > 0:
                    while int(time.time()) < close_time - ARB_DEADLINE_T:
                        other_price = get_token_price(token_other_id, "BUY")
                        if other_price is not None and other_price <= max_other_price:
                            cost_second = shares * other_price
                            total_cost = bet_size + cost_second
                            bet_pct_arb = (total_cost / config.bankroll) * 100 if config.bankroll else 0
                            print(f"  [{market.upper()}] DRY RUN: {trade_direction.upper()} @ ${token_price:.2f} + hedge @ ${other_price:.2f} | aposta: {bet_pct_arb:.1f}% da banca", flush=True)
                            _last_bet_window_by_market[market] = window_ts
                            return True
                        time.sleep(ARB_POLL_INTERVAL)
            if active_mode == "only_hedge_plus" and final_result is not None:
                p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
                dynamic_margin = _dynamic_ev_margin(final_result)
                if p_win <= token_price + dynamic_margin:
                    print(f"  [{market.upper()}] EV+ não mais válido: P(win)={p_win:.1%} <= preço ${token_price:.2f}+margem {dynamic_margin:.0%}, pulando.", flush=True)
                    return False
            bet_pct = (bet_size / config.bankroll) * 100 if config.bankroll else 0
            ev_edge = ""
            if active_mode == "only_hedge_plus" and final_result is not None:
                p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
                edge_pct = (p_win - token_price) * 100
                ev_edge = f" | EV+ edge: {edge_pct:.1f}%"
            print(f"  [{market.upper()}] DRY RUN: {trade_direction.upper()} @ ${token_price:.2f}, {shares:.2f} shares | aposta: {bet_pct:.1f}% da banca{ev_edge}", flush=True)
            _last_bet_window_by_market[market] = window_ts
            return True

    if not tokens:
        print(f"  [{market.upper()}] Mercado não encontrado na Polymarket.", flush=True)
        return False

    from api import get_token_price
    token_id = tokens[0] if trade_direction == "up" else tokens[1]
    real_price = get_token_price(token_id, "BUY")
    if real_price is None and event:
        from api import get_token_price_from_event
        real_price = get_token_price_from_event(event, trade_direction)
    # Log de operação real (auditoria)
    print(f"  [{market.upper()}] REAL | modo={active_mode} janela={window_ts} {trade_direction.upper()} ${bet_size:.2f}", flush=True)
    if active_mode == "moon" and real_price is None:
        print(f"  [{market.upper()}] MOON: preço do token indisponível, pulando.", flush=True)
        return False
    if real_price is not None and real_price > MAX_TOKEN_PRICE and active_mode != "arbitragem":
        print(
            f"  [{market.upper()}] Token @ ${real_price:.2f} > 90c, aguardando cair até ${MAX_TOKEN_PRICE:.2f} (até o fim da janela).",
            flush=True,
        )
        # Aguarda preço voltar dentro do limite até o fechamento
        while int(time.time()) < close_time:
            time.sleep(ORDER_RETRY_INTERVAL)
            real_price = get_token_price(token_id, "BUY")
            if real_price is None and event:
                from api import get_token_price_from_event
                real_price = get_token_price_from_event(event, trade_direction)
            if real_price is not None and real_price <= MAX_TOKEN_PRICE:
                break
        if real_price is None or real_price > MAX_TOKEN_PRICE:
            print(f"  [{market.upper()}] Token continuou > ${MAX_TOKEN_PRICE:.2f} até o fim da janela, pulando.", flush=True)
            return False
    if active_mode == "moon" and real_price is not None and real_price < MOON_MIN_ODD:
        print(f"  [{market.upper()}] MOON: Token @ ${real_price:.2f} < ${MOON_MIN_ODD:.2f}, pulando.", flush=True)
        return False
    # Modo 90-95: só executar ordem real se preço ainda estiver entre 80c e 95c (revalidação antes de enviar)
    if active_mode == "90_95":
        if real_price is None:
            print(f"  [{market.upper()}] 90-95: preço do token indisponível, pulando.", flush=True)
            return False
        if real_price < MODE_90_95_MIN_ODD or real_price > MODE_90_95_MAX_ODD:
            print(f"  [{market.upper()}] 90-95: preço atual ${real_price:.2f} fora da faixa 80–95c, não enviando ordem.", flush=True)
            return False
    if active_mode == "only_hedge_plus" and final_result is not None and real_price is not None:
        p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
        dynamic_margin = _dynamic_ev_margin(final_result)
        if p_win <= real_price + dynamic_margin:
            print(f"  [{market.upper()}] EV+ não mais válido: P(win)={p_win:.1%} <= preço ${real_price:.2f}+margem {dynamic_margin:.0%}, pulando.", flush=True)
            return False

    # Cada mercado pode apostar uma vez na sua janela; não travar outros mercados (btc/eth/btc15m independentes)
    # 6. Executar ordem(s)
    client = create_clob_client()
    # Sync agressivo para proxy/safe: API pode precisar de múltiplas chamadas para reconhecer allowance
    for _ in range(2):
        _sync_balance_allowance(client)
        time.sleep(1)
    ok = False
    arb_first_one_fill = False
    arb_shares = None

    try:
        if active_mode == "arbitragem" and tokens and len(tokens) == 2:
            price_up = get_token_price(tokens[0], "BUY")
            price_down = get_token_price(tokens[1], "BUY")
            if (price_up is not None and price_down is not None
                    and 0 < price_up < 1 and 0 < price_down < 1
                    and (price_up + price_down) <= (1.0 - ARB_MIN_PROFIT_PCT)):
                amount_up = bet_size * price_up / (price_up + price_down)
                amount_down = bet_size * price_down / (price_up + price_down)
                if amount_up >= POLY_MIN_ORDER_USD and amount_down >= POLY_MIN_ORDER_USD:
                    ok1 = place_fok_order(client, tokens[0], amount_up) or place_limit_order(client, tokens[0], amount_up)
                    ok2 = place_fok_order(client, tokens[1], amount_down) or place_limit_order(client, tokens[1], amount_down)
                    if ok1 and ok2:
                        print(f"  [{market.upper()}] ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} | executado", flush=True)
                        _tg_send(
                            f"[{market.upper()}] ARB PURA: UP @ {price_up:.2f} + DOWN @ {price_down:.2f} | "
                            f"bet ${bet_size:.2f}"
                        )
                        _last_bet_window_by_market[market] = window_ts
                        return True
                    if ok1 and not ok2:
                        ok = True
                        real_price = price_up
                        token_id = tokens[0]
                        arb_first_one_fill = True
                        arb_shares = bet_size / (price_up + price_down)
                        trade_direction = "up"
                else:
                    print(f"  [{market.upper()}] Arb: cada perna precisa >= ${POLY_MIN_ORDER_USD:.2f} (Up ${amount_up:.2f} / Down ${amount_down:.2f}). Aumente a aposta.", flush=True)
                    if trade_direction == "arb_pura":
                        return False
            elif trade_direction == "arb_pura":
                print(f"  [{market.upper()}] Arb sumiu ao executar (soma > {1.0 - ARB_MIN_PROFIT_PCT:.0%}), pulando.", flush=True)
                return False

        if not ok and trade_direction != "arb_pura":
            for _ in range(ORDER_MAX_FOK_RETRIES):
                if int(time.time()) >= close_time:
                    break
                try:
                    ok = place_fok_order(client, token_id, bet_size)
                except Exception as e:
                    if "Request exception" in str(e):
                        if _poly_has_recent_trade(client, token_id):
                            print(f"  [{market.upper()}] Ordem confirmada após erro de rede (trade recente).", flush=True)
                            _last_bet_window_by_market[market] = window_ts
                            return True
                        print(f"  [{market.upper()}] Erro de rede; sem trade recente, tentando novamente...", flush=True)
                        time.sleep(ORDER_RETRY_INTERVAL)
                        continue
                    raise
                if ok:
                    break
                if _poly_has_recent_trade(client, token_id):
                    print(f"  [{market.upper()}] Ordem confirmada após falha FOK (trade recente).", flush=True)
                    _last_bet_window_by_market[market] = window_ts
                    return True
                time.sleep(ORDER_RETRY_INTERVAL)
            if not ok:
                if _poly_has_recent_trade(client, token_id):
                    print(f"  [{market.upper()}] Ordem confirmada antes do limit (trade recente).", flush=True)
                    _last_bet_window_by_market[market] = window_ts
                    return True
                ok = place_limit_order(client, token_id, bet_size)
                if not ok and _poly_has_recent_trade(client, token_id):
                    print(f"  [{market.upper()}] Ordem confirmada após limit (trade recente).", flush=True)
                    _last_bet_window_by_market[market] = window_ts
                    return True
    except Exception as e:
        if "Request exception" in str(e):
            if _poly_has_recent_trade(client, token_id):
                print(f"  [{market.upper()}] Ordem confirmada após erro de rede (trade recente).", flush=True)
                _last_bet_window_by_market[market] = window_ts
                return True
            print(f"  [{market.upper()}] Erro de requisição; sem trade recente, continuando até o fim da janela.", flush=True)
            return False
        raise

    if ok:
        print(f"  [{market.upper()}] Ordem executada: {trade_direction.upper()} ${bet_size:.2f}", flush=True)
        if trade_direction:
            if real_price is not None:
                _tg_send(
                    f"[{market.upper()}] {active_mode.upper()} {trade_direction.upper()} @ {real_price:.2f} | "
                    f"bet ${bet_size:.2f}"
                )
            else:
                _tg_send(
                    f"[{market.upper()}] {active_mode.upper()} {trade_direction.upper()} | bet ${bet_size:.2f}"
                )
        if active_mode == "arbitragem" and real_price is not None and tokens and len(tokens) == 2:
            other_token_id = tokens[1] if trade_direction == "up" else tokens[0]
            buy_price = real_price
            shares_arb = arb_shares if arb_first_one_fill and arb_shares is not None else bet_size / buy_price
            max_other_price = (1.0 - ARB_MIN_PROFIT_PCT) - buy_price
            if max_other_price > 0:
                while int(time.time()) < close_time - ARB_DEADLINE_T:
                    other_price = get_token_price(other_token_id, "BUY")
                    if other_price is not None and other_price <= max_other_price:
                        amount_second = shares_arb * other_price
                        if amount_second >= POLY_MIN_ORDER_USD:
                            side_second = "Down" if trade_direction == "up" else "Up"
                            print(f"  [{market.upper()}] REAL | Hedge: {side_second} @ ${other_price:.2f} | ${amount_second:.2f}", flush=True)
                            ok2 = place_fok_order(client, other_token_id, amount_second)
                            if not ok2:
                                ok2 = place_limit_order(client, other_token_id, amount_second)
                            if ok2:
                                print(f"  [{market.upper()}] ARBITRAGEM: comprado lado oposto @ ${other_price:.2f} | executado", flush=True)
                                _tg_send(
                                    f"[{market.upper()}] ARBITRAGEM: hedge {side_second.upper()} @ {other_price:.2f} | "
                                    f"${amount_second:.2f}"
                                )
                                _last_bet_window_by_market[market] = window_ts
                                return True
                    time.sleep(ARB_POLL_INTERVAL)
    else:
        print(f"  [{market.upper()}] Falha ao executar ordem. (Verifique: allowance, saldo USDC, conexão.)", flush=True)

    if ok:
        _last_bet_window_by_market[market] = window_ts
    return ok


def _market_loop(
    config: Config,
    market: str,
    shared: dict,
    startup_mode: str,
) -> None:
    """Roda ciclos para um único mercado em loop (para execução paralela). startup_mode = modo fixado no arranque."""
    while not shared.get("stop"):
        try:
            if run_trade_cycle(config, market, active_mode=startup_mode, shared=shared):
                with shared["trades_lock"]:
                    shared["trades"] += 1
                    if config.max_trades and shared["trades"] >= config.max_trades:
                        shared["stop"] = True
            if config.once:
                break
            if config.dry_run:
                with _bankroll_lock:
                    if config.bankroll < config.min_bet:
                        config.bankroll = config.original_bankroll
        except Exception as e:
            print(f"  [{market.upper()}] Erro no loop: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)


def main():
    global FROZEN_MODE
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Min Up/Down Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simular sem ordens reais")
    parser.add_argument("--mode", choices=["safe", "spike_ai", "moon", "multi_confirm", "aggressive", "degen", "arbitragem", "arb_kalshi", "only_hedge_plus", "odd_master", "90_95"], help="Modo de trading")
    parser.add_argument("--safe-bet", type=float, metavar="USD", help="Modo safe: valor fixo em USD por entrada")
    parser.add_argument("--only-hedge-bet", type=float, metavar="USD", help="Modo only_hedge_plus: valor fixo em USD por entrada")
    parser.add_argument("--odd-master-bet", type=float, metavar="USD", help="Modo odd_master: valor fixo em USD por entrada")
    parser.add_argument("--bet-90-95", type=float, metavar="USD", help="Modo 90-95: valor fixo em USD por entrada (janela 20s–2s, odd 80–95c)")
    parser.add_argument("--arbitragem-pct", type=float, metavar="PCT", help="Modo arbitragem: %% da banca por entrada (ex: 25)")
    parser.add_argument("--once", action="store_true", help="Apenas um ciclo")
    parser.add_argument("--max-trades", type=int, help="Máximo de trades (dry-run)")
    parser.add_argument("--markets", type=str, metavar="LIST", help="Mercados: btc, eth, btc15m, eth15m (ex: btc,btc15m ou both para btc+eth)")
    args = parser.parse_args()

    # Diagnóstico: o que o processo recebeu (para conferir se web/CLI passou --mode certo)
    print(f"CLI recebido: --mode={args.mode!r} | BOT_MODE(env)={os.getenv('BOT_MODE', '(não definido)')!r}", flush=True)

    config = load_config()
    # --markets define exatamente em quais mercados operar; nenhum outro é adicionado (ex.: só btc15m = não opera em btc 5min)
    if args.markets:
        raw = args.markets.strip().lower()
        if raw == "both":
            config.markets = ["btc", "eth"]
        elif "," in raw:
            config.markets = [m.strip() for m in raw.split(",") if m.strip() in ("btc", "eth", "btc15m", "eth15m")]
        elif raw in ("btc", "eth", "btc15m"):
            config.markets = [raw]
        else:
            config.markets = []
    if not config.markets:
        print("Erro: nenhum mercado selecionado. Use --markets btc,eth,btc15m,eth15m (ou pelo menos um).", flush=True)
        sys.exit(1)
    # Modo: prioridade para --mode; se não veio pela CLI, usar BOT_MODE do ambiente (web define ao iniciar)
    config.mode = args.mode if args.mode is not None else os.getenv("BOT_MODE", "safe")
    FROZEN_MODE = config.mode  # fixar AGORA; nenhum ciclo pode usar outro modo
    if args.mode is None:
        print(f"Modo definido pelo ambiente (BOT_MODE): {config.mode}", flush=True)
    config.dry_run = args.dry_run
    config.once = args.once
    config.max_trades = args.max_trades

    if config.mode == "only_hedge_plus":
        if args.only_hedge_bet is not None:
            v = args.only_hedge_bet
            if v < config.min_bet:
                print(f"Erro: --only-hedge-bet deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            config.fixed_bet_only_hedge = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print(f"Only Hedge+: valor fixo em USD [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        continue
                    v = float(s.replace(",", "."))
                    if v < config.min_bet:
                        continue
                    config.fixed_bet_only_hedge = v
                    break
                except ValueError:
                    pass
        else:
            print("Only Hedge+ exige valor. Use --only-hedge-bet 5.0", flush=True, file=sys.stderr)
            sys.exit(1)

    if config.mode == "odd_master":
        if args.odd_master_bet is not None:
            v = args.odd_master_bet
            if v < config.min_bet:
                print(f"Erro: --odd-master-bet deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            config.fixed_bet_odd_master = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print(f"ODD MASTER: valor fixo em USD [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        continue
                    v = float(s.replace(",", "."))
                    if v < config.min_bet:
                        continue
                    config.fixed_bet_odd_master = v
                    break
                except ValueError:
                    pass
        else:
            v = float(os.getenv("ODD_MASTER_BET", "0") or "0")
            if v >= config.min_bet:
                config.fixed_bet_odd_master = v
            else:
                print("ODD MASTER exige valor. Use --odd-master-bet 5.0 ou ODD_MASTER_BET no env.", flush=True, file=sys.stderr)
                sys.exit(1)

    if config.mode == "90_95":
        if args.bet_90_95 is not None:
            v = args.bet_90_95
            if v < config.min_bet:
                print(f"Erro: --bet-90-95 deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            config.fixed_bet_90_95 = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print(f"90-95: valor fixo em USD [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        continue
                    v = float(s.replace(",", "."))
                    if v < config.min_bet:
                        continue
                    config.fixed_bet_90_95 = v
                    break
                except ValueError:
                    pass
        else:
            v = float(os.getenv("BET_90_95", "0") or "0")
            if v >= config.min_bet:
                config.fixed_bet_90_95 = v
            else:
                print("90-95 exige valor. Use --bet-90-95 5.0 ou BET_90_95 no env.", flush=True, file=sys.stderr)
                sys.exit(1)

    if config.mode in ("safe", "spike_ai", "moon", "multi_confirm"):
        if args.safe_bet is not None:
            v = args.safe_bet
            if v < config.min_bet:
                print(f"Erro: --safe-bet deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            config.fixed_bet_safe = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    label = "Modo safe" if config.mode == "safe" else ("Modo SPIKE AI" if config.mode == "spike_ai" else ("Modo MOON" if config.mode == "moon" else "Modo MC+RD"))
                    print(f"{label}: valor fixo em USD [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        continue
                    v = float(s.replace(",", "."))
                    if v < config.min_bet:
                        continue
                    config.fixed_bet_safe = v
                    break
                except ValueError:
                    pass
        else:
            print("Modo safe/SPIKE AI/MOON exige valor. Use --safe-bet 5.0", flush=True, file=sys.stderr)
            sys.exit(1)

    if config.mode in ("arbitragem", "arb_kalshi"):
        if args.arbitragem_pct is not None:
            pct = args.arbitragem_pct
            if pct < 1 or pct > 100:
                print("Erro: --arbitragem-pct deve ser entre 1 e 100", flush=True)
                sys.exit(1)
            config.arbitragem_bet_pct = pct / 100.0
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print("Modo arbitragem: % da banca [1-100]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        continue
                    pct = float(s.replace(",", "."))
                    if pct < 1 or pct > 100:
                        continue
                    config.arbitragem_bet_pct = pct / 100.0
                    break
                except ValueError:
                    pass
        else:
            print("Modo arbitragem exige %%. Use --arbitragem-pct 25", flush=True, file=sys.stderr)
            sys.exit(1)

    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    funder = (os.getenv("POLY_FUNDER_ADDRESS") or "").strip()
    funder_info = f"funder=0x...{funder[-4:]}" if len(funder) >= 4 else ("funder=nenhum" if not funder else "funder=ok")
    print(f"Wallet: signature_type={sig_type} ({'EOA' if sig_type == 0 else 'Magic' if sig_type == 1 else 'Proxy/Safe'}) | {funder_info}", flush=True)
    if funder and sig_type == 0:
        print("  AVISO: Funder definido mas signature_type=0. Para Proxy/Safe use tipo 2 na Config.", flush=True)
    markets_str = "+".join(m.upper() for m in config.markets)
    print(f"Polymarket {markets_str} Bot | Modo: {config.mode} | Dry-run: {config.dry_run}", flush=True)
    print(f"Min bet: ${config.min_bet:.2f} | Max token: 98c", flush=True)
    print(f"Estratégias assertivas: Spike salto>={SPIKE_THRESHOLD} + conf>={SPIKE_MIN_CONFIDENCE:.0%} | Confiança por modo | T-5s conf>={T5S_MIN_CONFIDENCE:.0%}", flush=True)
    if config.mode == "safe" and config.fixed_bet_safe is not None:
        print(f"Entrada fixa (safe): ${config.fixed_bet_safe:.2f}", flush=True)
    if config.mode == "spike_ai" and config.fixed_bet_safe is not None:
        print(f"SPIKE AI: igual ao safe + gate de IA (Ollama) | entrada fixa ${config.fixed_bet_safe:.2f} | AI_MIN_CONFIDENCE={AI_MIN_CONFIDENCE:.0%}", flush=True)
    if config.mode == "moon" and config.fixed_bet_safe is not None:
        print(f"MOON: estratégia CVD (divergência + momentum) com mão fixa safe | entrada fixa ${config.fixed_bet_safe:.2f}", flush=True)
    if config.mode == "multi_confirm":
        print("MC+RD: Multi-Confirmacao + Regime + Divergencia | apenas sinais (sem ordens)", flush=True)
    if config.mode == "only_hedge_plus":
        print(f"Only Hedge+: entrada fixa ${config.fixed_bet_only_hedge or config.min_bet:.2f} | só entra com EV+", flush=True)
    if config.mode == "odd_master":
        print(f"ODD MASTER: entrada fixa ${config.fixed_bet_odd_master or config.min_bet:.2f} | últimos {ODD_MASTER_LAST_SEC}s, price-to-beat ±${ODD_MASTER_MAX_DIFF_USD:.0f}, maior odd", flush=True)
    if config.mode == "90_95":
        print(f"90-95: entrada fixa ${config.fixed_bet_90_95 or config.min_bet:.2f} | janela 20s–2s p/ close, maior preço entre {MODE_90_95_MIN_ODD:.0%} e {MODE_90_95_MAX_ODD:.0%}", flush=True)
    if config.mode in ("arbitragem", "arb_kalshi") and config.arbitragem_bet_pct is not None:
        print(f"Arbitragem: {config.arbitragem_bet_pct * 100:.0f}% da banca (via API)", flush=True)
    if config.mode == "aggressive":
        print(f"Agressivo: {MODES['aggressive']['bet_pct']*100:.0f}% da banca (via API)", flush=True)
    print(f"Mercados: {markets_str}", flush=True)
    print("-" * 50, flush=True)

    shared = {"stop": False, "trades": 0, "trades_lock": threading.Lock()}
    startup_mode = config.mode
    FROZEN_MODE = startup_mode  # fonte única de verdade para todos os ciclos
    print(f"Modo fixado para esta execução: {startup_mode}", flush=True)
    threads = [
        threading.Thread(target=_market_loop, args=(config, market, shared, startup_mode), name=f"bot-{market}")
        for market in config.markets
    ]
    for t in threads:
        t.start()
    interrupt = False
    try:
        while not shared["stop"]:
            time.sleep(1)
            if config.max_trades and shared["trades"] >= config.max_trades:
                shared["stop"] = True
            if config.once and not any(t.is_alive() for t in threads):
                break
    except KeyboardInterrupt:
        interrupt = True
    shared["stop"] = True
    if config.max_trades and shared["trades"] >= config.max_trades:
        print(f"\nMax trades ({config.max_trades}) atingido.", flush=True)
    elif interrupt:
        print("\nEncerrado pelo usuário.", flush=True)
    for t in threads:
        t.join(timeout=15)
        if t.is_alive():
            print(f"  Aviso: thread {t.name} ainda rodando (timeout).", flush=True)


if __name__ == "__main__":
    main()
