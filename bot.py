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
        out = [m.strip().lower() for m in v.split(",") if m.strip() in ("btc", "eth", "btc15m")]
        return out if out else []
    if v == "both":
        return ["btc", "eth"]
    if v == "eth":
        return ["eth"]
    if v == "btc15m":
        return ["btc15m"]
    if v == "btc":
        return ["btc"]
    return []

# Configurações de modos (confiança mínima mais assertiva para reduzir entradas ruins de dia)
MODES = {
    "safe": {"min_confidence": 0.72},
    "aggressive": {"bet_pct": float(os.getenv("AGGRESSIVE_BET_PCT", "25")) / 100.0, "min_confidence": 0.58},
    "degen": {"bet_pct": 1.0, "min_confidence": 0.0},
    "arbitragem": {"min_confidence": 0.30},
    "only_hedge_plus": {"min_confidence": 0.72},
    "odd_master": {},  # Estratégia própria: últimos 10s, price-to-beat dentro de $10, maior odd
    "90_95": {},       # Últimos 10s, entra no lado de maior odd só se a odd estiver entre 90c e 95c; aposta fixa
}
EV_MIN_MARGIN = 0.02
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.04"))
ARB_POLL_INTERVAL = 1
ARB_DEADLINE_T = 10
ODD_MASTER_LAST_SEC = 10  # Entrar apenas nos últimos 10 segundos
ODD_MASTER_MAX_DIFF_USD = 10.0  # Price to beat entre 0 e $10 de diferença do preço atual
MODE_90_95_LAST_SEC = 10   # 90-95: últimos 10s
MODE_90_95_MIN_ODD = 0.90  # 90-95: odd mínima (90c)
MODE_90_95_MAX_ODD = 0.95  # 90-95: odd máxima (95c)

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
WINDOW_SEC = 300
WINDOW_SEC_15M = 900
MONITOR_START_T = 120
MONITOR_START_T_15M = 300
HARD_DEADLINE_T = 40
MIN_SECS_TO_ENTER = 40
TA_POLL_INTERVAL = 2
SPIKE_THRESHOLD = 2.5   # Salto mínimo de score para spike (mais assertivo; evita ruído de dia)
SPIKE_MIN_CONFIDENCE = 0.48  # Spike só dispara se confiança >= 48%
T5S_MIN_CONFIDENCE = 0.40   # T-5s só dispara se melhor sinal tiver confiança >= 40%
ORDER_RETRY_INTERVAL = 3
ORDER_MAX_FOK_RETRIES = 5  # Limite de retentativas FOK para não bloquear outros mercados (ex: ETH)
MIN_SHARES = 5
POLY_MIN_ORDER_USD = 1.0  # Polymarket exige mínimo $1 por ordem (marketable BUY)
LIMIT_FALLBACK_PRICE = 0.95
MAX_TOKEN_PRICE = float(os.getenv("MAX_TOKEN_PRICE", "0.98"))
# Resolução é por Chainlink: usar Price to Beat (abertura Chainlink) alinha TA ao resultado
USE_CHAINLINK_OPEN = (os.getenv("USE_CHAINLINK_OPEN", "1").strip().lower() in ("1", "true", "yes"))
MAX_DELTA_OPEN_USD = float(os.getenv("MAX_DELTA_OPEN_USD", "0"))  # 0 = não filtrar; >0 = pular se |delta Binance-Chainlink| > valor

# Última janela em que apostamos por mercado (evita repetir no mesmo mercado na mesma janela)
_last_bet_window_by_market: dict[str, int] = {}
_bankroll_lock = threading.Lock()
# Modo fixado no arranque (fonte única de verdade; evita safe quando iniciou em aggressive)
FROZEN_MODE: Optional[str] = None


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


def seconds_until_close(window_ts: int, window_sec: int) -> int:
    """Segundos até o fechamento da janela (window_ts + window_sec - now)."""
    return (window_ts + window_sec) - int(time.time())


def load_config() -> Config:
    bankroll = float(os.getenv("STARTING_BANKROLL", "10.0"))
    min_bet = float(os.getenv("MIN_BET", "5.0"))
    return Config(
        dry_run=False,
        mode=os.getenv("BOT_MODE", "safe"),
        once=False,
        max_trades=None,
        bankroll=bankroll,
        min_bet=min_bet,
        original_bankroll=bankroll,
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
        return False


def run_trade_cycle(config: Config, market: str, active_mode: Optional[str] = None, shared: Optional[dict] = None) -> bool:
    """Executa um ciclo para um mercado (btc, eth ou btc15m): espera, analisa, opera.
    active_mode: modo fixado no arranque (evita mistura safe/aggressive); se None, usa FROZEN_MODE ou config.mode.
    """
    global _last_bet_window_by_market
    from api import get_market_by_slug, extract_token_ids, get_price_by_market, get_candles_by_market, get_btc_candles_1m, get_price_to_beat, get_open_delta_binance_chainlink
    from strategy import analyze

    # Fonte única de verdade: SEMPRE usar FROZEN_MODE quando estiver definido (evita 2 compras com modos diferentes)
    if FROZEN_MODE is not None:
        active_mode = FROZEN_MODE
    elif active_mode is None:
        active_mode = config.mode
    is_15m = market == "btc15m"
    window_sec = WINDOW_SEC_15M if is_15m else WINDOW_SEC
    window_ts = get_window_ts_15m() if is_15m else get_window_ts()
    close_time = window_ts + window_sec
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

    slug = f"btc-updown-15m-{window_ts}" if is_15m else f"{market}-updown-5m-{window_ts}"

    # Não apostar mais de uma vez na mesma janela por mercado (1 compra por mercado por janela)
    if _last_bet_window_by_market.get(market) == window_ts:
        return False

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

    # Delta Binance vs Chainlink: resolução é por Chainlink; usar Price to Beat alinha TA ao resultado
    delta_info = get_open_delta_binance_chainlink(slug, market, window_ts, is_15m)
    if delta_info and USE_CHAINLINK_OPEN and delta_info.get("chainlink_open") is not None:
        window_open = delta_info["chainlink_open"]
        d_usd = delta_info.get("delta_usd", 0)
        d_pct = delta_info.get("delta_pct", 0)
        print(f"  [{market.upper()}] Abertura Chainlink (Price to Beat) ${window_open:.2f} | delta Binance–Chainlink: ${d_usd:+.2f} ({d_pct:+.3f}%)", flush=True)
        if MAX_DELTA_OPEN_USD > 0 and abs(d_usd) > MAX_DELTA_OPEN_USD:
            print(f"  [{market.upper()}] Delta |${d_usd:.2f}| > {MAX_DELTA_OPEN_USD:.0f} USD, pulando janela.", flush=True)
            return False
    elif delta_info and not USE_CHAINLINK_OPEN:
        d_usd = delta_info.get("delta_usd", 0)
        print(f"  [{market.upper()}] Abertura Binance ${window_open:.2f} | delta Binance–Chainlink: ${d_usd:+.2f} (Chainlink não usado)", flush=True)

    tick_prices = []
    best_score = 0.0
    best_result = None
    prev_score = 0.0
    fired = False
    trade_direction = None
    final_result = None

    # Para ODD MASTER e 90-95: loop até 10s antes do close; para outros modos até 40s antes
    deadline_sec = ODD_MASTER_LAST_SEC if active_mode == "odd_master" else (MODE_90_95_LAST_SEC if active_mode == "90_95" else HARD_DEADLINE_T)

    while int(time.time()) < close_time - deadline_sec:
        # Modo ODD MASTER: últimos 10s, price-to-beat entre 0 e $10 do preço atual, entrar no lado de maior odd
        if active_mode == "odd_master" and tokens and len(tokens) == 2 and event:
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev
            current_price = get_price_by_market(market)
            if current_price is not None and window_open is not None:
                diff_usd = abs(float(current_price) - float(window_open))
                if 0 <= diff_usd <= ODD_MASTER_MAX_DIFF_USD:
                    price_up = _get_tok(tokens[0], "BUY") or _get_ev(event, "up")
                    price_down = _get_tok(tokens[1], "BUY") or _get_ev(event, "down")
                    if price_up is not None and price_down is not None and 0 < price_up < 1 and 0 < price_down < 1:
                        fired = True
                        trade_direction = "up" if price_up >= price_down else "down"
                        odd = price_up if trade_direction == "up" else price_down
                        print(f"  [{market.upper()}] ODD MASTER: price-to-beat ${window_open:.2f} vs atual ${current_price:.2f} (diff ${diff_usd:.2f}) | maior odd = {trade_direction.upper()} @ {odd:.2f}", flush=True)
                        break
            time.sleep(1)
            continue

        # Modo 90-95: últimos 10s, entra no lado de maior odd só se a odd estiver entre 90c e 95c
        if active_mode == "90_95" and tokens and len(tokens) == 2 and event:
            from api import get_token_price as _get_tok, get_token_price_from_event as _get_ev
            price_up = _get_tok(tokens[0], "BUY") or _get_ev(event, "up")
            price_down = _get_tok(tokens[1], "BUY") or _get_ev(event, "down")
            if price_up is not None and price_down is not None and 0 < price_up < 1 and 0 < price_down < 1:
                if price_up >= price_down:
                    odd = price_up
                    if MODE_90_95_MIN_ODD <= odd <= MODE_90_95_MAX_ODD:
                        fired = True
                        trade_direction = "up"
                        print(f"  [{market.upper()}] 90-95: maior odd = UP @ {odd:.2f} (entre 90–95c)", flush=True)
                        break
                else:
                    odd = price_down
                    if MODE_90_95_MIN_ODD <= odd <= MODE_90_95_MAX_ODD:
                        fired = True
                        trade_direction = "down"
                        print(f"  [{market.upper()}] 90-95: maior odd = DOWN @ {odd:.2f} (entre 90–95c)", flush=True)
                        break
            time.sleep(1)
            continue

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

        price = get_price_by_market(market)
        if price:
            tick_prices.append(price)
        # Para btc15m usar candles 1m na TA para que todos os 7 indicadores da estratégia rodem
        candles = get_btc_candles_1m(limit=30) if is_15m else get_candles_by_market(market, limit=30)
        if not candles:
            time.sleep(ARB_POLL_INTERVAL if active_mode == "arbitragem" else TA_POLL_INTERVAL)
            continue

        result = analyze(window_open, price or candles[-1]["c"], candles, tick_prices[-20:] if tick_prices else None)

        if abs(result.score) > abs(best_score):
            best_score = result.score
            best_result = result

        if abs(result.score - prev_score) >= SPIKE_THRESHOLD and prev_score != 0:
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
                    fired = True
                    print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction}", flush=True)
                    break
            else:
                print(f"  [{market.upper()}] SPIKE ignorado (confiança {result.confidence:.1%} < {SPIKE_MIN_CONFIDENCE:.0%})", flush=True)

        mode_cfg = MODES.get(active_mode, MODES["safe"])
        if result.confidence >= mode_cfg["min_confidence"]:
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
                fired = True
                print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction}", flush=True)
                break

        prev_score = result.score
        poll = ARB_POLL_INTERVAL if active_mode == "arbitragem" else TA_POLL_INTERVAL
        time.sleep(poll)

    if not fired and best_result:
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
        else:
            fired = True
            print(f"  [{market.upper()}] T-5s: melhor sinal -> {trade_direction} (score {best_score:.2f})", flush=True)

    if not fired or not trade_direction:
        print(f"  [{market.upper()}] Sem sinal válido, pulando janela.", flush=True)
        return False

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

    if active_mode == "safe" and config.fixed_bet_safe is not None:
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
    # Log de operação real (auditoria)
    print(f"  [{market.upper()}] REAL | modo={active_mode} janela={window_ts} {trade_direction.upper()} ${bet_size:.2f}", flush=True)
    if real_price is not None and real_price > MAX_TOKEN_PRICE and active_mode != "arbitragem":
        print(f"  [{market.upper()}] Token @ ${real_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
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
            ok = place_fok_order(client, token_id, bet_size)
            if ok:
                break
            time.sleep(ORDER_RETRY_INTERVAL)
        if not ok:
            ok = place_limit_order(client, token_id, bet_size)

    if ok:
        print(f"  [{market.upper()}] Ordem executada: {trade_direction.upper()} ${bet_size:.2f}", flush=True)
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
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen", "arbitragem", "only_hedge_plus", "odd_master", "90_95"], help="Modo de trading")
    parser.add_argument("--safe-bet", type=float, metavar="USD", help="Modo safe: valor fixo em USD por entrada")
    parser.add_argument("--only-hedge-bet", type=float, metavar="USD", help="Modo only_hedge_plus: valor fixo em USD por entrada")
    parser.add_argument("--odd-master-bet", type=float, metavar="USD", help="Modo odd_master: valor fixo em USD por entrada")
    parser.add_argument("--bet-90-95", type=float, metavar="USD", help="Modo 90-95: valor fixo em USD por entrada (últimos 10s, odd entre 90c e 95c)")
    parser.add_argument("--arbitragem-pct", type=float, metavar="PCT", help="Modo arbitragem: %% da banca por entrada (ex: 25)")
    parser.add_argument("--once", action="store_true", help="Apenas um ciclo")
    parser.add_argument("--max-trades", type=int, help="Máximo de trades (dry-run)")
    parser.add_argument("--markets", type=str, metavar="LIST", help="Mercados: btc, eth, btc15m (ex: btc,btc15m ou both para btc+eth)")
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
            config.markets = [m.strip() for m in raw.split(",") if m.strip() in ("btc", "eth", "btc15m")]
        elif raw in ("btc", "eth", "btc15m"):
            config.markets = [raw]
        else:
            config.markets = []
    if not config.markets:
        print("Erro: nenhum mercado selecionado. Use --markets btc,eth,btc15m (ou pelo menos um).", flush=True)
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

    if config.mode == "safe":
        if args.safe_bet is not None:
            v = args.safe_bet
            if v < config.min_bet:
                print(f"Erro: --safe-bet deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            config.fixed_bet_safe = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print(f"Modo safe: valor fixo em USD [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
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
            print("Modo safe exige valor. Use --safe-bet 5.0", flush=True, file=sys.stderr)
            sys.exit(1)

    if config.mode == "arbitragem":
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
    if config.mode == "only_hedge_plus":
        print(f"Only Hedge+: entrada fixa ${config.fixed_bet_only_hedge or config.min_bet:.2f} | só entra com EV+", flush=True)
    if config.mode == "odd_master":
        print(f"ODD MASTER: entrada fixa ${config.fixed_bet_odd_master or config.min_bet:.2f} | últimos {ODD_MASTER_LAST_SEC}s, price-to-beat ±${ODD_MASTER_MAX_DIFF_USD:.0f}, maior odd", flush=True)
    if config.mode == "90_95":
        print(f"90-95: entrada fixa ${config.fixed_bet_90_95 or config.min_bet:.2f} | últimos {MODE_90_95_LAST_SEC}s, maior odd entre {MODE_90_95_MIN_ODD:.0%} e {MODE_90_95_MAX_ODD:.0%}", flush=True)
    if config.mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        print(f"Arbitragem: {config.arbitragem_bet_pct * 100:.0f}% da banca (via API) | Sem oportunidade = aposta normal", flush=True)
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
