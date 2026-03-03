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
from dataclasses import dataclass, field
from typing import Optional

from dotenv import load_dotenv

# Mercados: btc, eth, btc15m (combinações via lista)
def _parse_markets() -> list[str]:
    v = (os.getenv("BOT_MARKETS") or "btc").strip().lower()
    if "," in v:
        return [m.strip().lower() for m in v.split(",") if m.strip() in ("btc", "eth", "btc15m")]
    if v == "both":
        return ["btc", "eth"]
    if v == "eth":
        return ["eth"]
    if v == "btc15m":
        return ["btc15m"]
    return ["btc"]

load_dotenv()

# Configurações de modos
MODES = {
    "safe": {"min_confidence": 0.50},
    "aggressive": {"bet_pct": float(os.getenv("AGGRESSIVE_BET_PCT", "25")) / 100.0, "min_confidence": 0.50},
    "degen": {"bet_pct": 1.0, "min_confidence": 0.0},
    "arbitragem": {"min_confidence": 0.30},
    "only_hedge_plus": {"min_confidence": 0.50},
}
EV_MIN_MARGIN = 0.02
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.04"))
ARB_POLL_INTERVAL = 1
ARB_DEADLINE_T = 10

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
WINDOW_SEC = 300
WINDOW_SEC_15M = 900
MONITOR_START_T = 120
MONITOR_START_T_15M = 300
HARD_DEADLINE_T = 40
MIN_SECS_TO_ENTER = 40
TA_POLL_INTERVAL = 2
SPIKE_THRESHOLD = 1.5
ORDER_RETRY_INTERVAL = 3
ORDER_MAX_FOK_RETRIES = 5  # Limite de retentativas FOK para não bloquear outros mercados (ex: ETH)
MIN_SHARES = 5
LIMIT_FALLBACK_PRICE = 0.95
MAX_TOKEN_PRICE = float(os.getenv("MAX_TOKEN_PRICE", "0.98"))

# Última janela em que apostamos por mercado (safe, only_hedge_plus, aggressive)
_last_bet_window_by_market: dict[str, int] = {}


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
        raise ValueError("Defina POLY_PRIVATE_KEY no .env")
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


def run_trade_cycle(config: Config, market: str) -> bool:
    """Executa um ciclo para um mercado (btc, eth ou btc15m): espera, analisa, opera."""
    global _last_bet_window_by_market
    from api import get_market_by_slug, extract_token_ids, get_price_by_market, get_candles_by_market
    from strategy import analyze

    is_15m = market == "btc15m"
    window_sec = WINDOW_SEC_15M if is_15m else WINDOW_SEC
    window_ts = get_window_ts_15m() if is_15m else get_window_ts()
    slug = f"btc-updown-15m-{window_ts}" if is_15m else f"{market}-updown-5m-{window_ts}"
    close_time = window_ts + window_sec
    monitor_secs = (
        MONITOR_START_T_15M if is_15m else (WINDOW_SEC if config.mode == "arbitragem" else MONITOR_START_T)
    )

    # Safe, only_hedge_plus, aggressive: não apostar mais de uma vez na mesma janela por mercado
    if config.mode in ("safe", "only_hedge_plus", "aggressive") and _last_bet_window_by_market.get(market) == window_ts:
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

    tick_prices = []
    best_score = 0.0
    best_result = None
    prev_score = 0.0
    fired = False
    trade_direction = None
    final_result = None

    while int(time.time()) < close_time - HARD_DEADLINE_T:
        price = get_price_by_market(market)
        if price:
            tick_prices.append(price)
        candles = get_candles_by_market(market, limit=30)
        if not candles:
            time.sleep(TA_POLL_INTERVAL)
            continue

        result = analyze(window_open, price or candles[-1]["c"], candles, tick_prices[-20:] if tick_prices else None)

        if abs(result.score) > abs(best_score):
            best_score = result.score
            best_result = result

        if abs(result.score - prev_score) >= SPIKE_THRESHOLD and prev_score != 0:
            trade_direction = result.direction
            final_result = result
            if config.mode == "only_hedge_plus":
                if _check_ev_plus(trade_direction, final_result, tokens, event):
                    fired = True
                    print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction} (EV+)", flush=True)
                    break
            else:
                fired = True
                print(f"  [{market.upper()}] SPIKE! Score {result.score:.2f} -> {result.direction}", flush=True)
                break

        mode_cfg = MODES.get(config.mode, MODES["safe"])
        if result.confidence >= mode_cfg["min_confidence"]:
            trade_direction = result.direction
            final_result = result
            if config.mode == "only_hedge_plus":
                if _check_ev_plus(trade_direction, final_result, tokens, event):
                    fired = True
                    print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction} (EV+)", flush=True)
                    break
            else:
                fired = True
                print(f"  [{market.upper()}] Confiança {result.confidence:.1%} -> {result.direction}", flush=True)
                break

        prev_score = result.score
        time.sleep(TA_POLL_INTERVAL)

    if not fired and best_result:
        trade_direction = best_result.direction
        final_result = best_result
        if config.mode == "only_hedge_plus":
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

    if config.mode == "safe" and config.fixed_bet_safe is not None:
        bet_size = config.fixed_bet_safe
    elif config.mode == "only_hedge_plus":
        bet_size = config.fixed_bet_only_hedge if config.fixed_bet_only_hedge is not None else config.min_bet
    elif config.mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        bankroll = api_bankroll if api_bankroll is not None else config.bankroll
        bet_size = bankroll * config.arbitragem_bet_pct
    elif config.mode == "aggressive":
        mode_cfg = MODES.get(config.mode, MODES["safe"])
        bankroll = api_bankroll if api_bankroll is not None else config.bankroll
        bet_size = bankroll * mode_cfg.get("bet_pct", 0.25)
    else:
        bet_size = (api_bankroll or config.bankroll) * MODES.get(config.mode, MODES["safe"]).get("bet_pct", 0.25)

    bet_size = max(bet_size, config.min_bet)
    cap = api_bankroll if api_bankroll is not None else config.bankroll
    if bet_size > cap:
        bet_size = cap

    if config.mode in ("aggressive", "arbitragem") and api_bankroll is not None:
        pct = (config.arbitragem_bet_pct * 100) if config.mode == "arbitragem" else (MODES.get(config.mode, MODES["safe"]).get("bet_pct", 0.25) * 100)
        print(f"  [{market.upper()}] Saldo API: ${api_bankroll:.2f} | aposta {pct:.0f}% = ${bet_size:.2f}", flush=True)

    if bet_size < config.min_bet:
        print(f"  [{market.upper()}] Bankroll insuficiente: ${cap:.2f} < min ${config.min_bet:.2f}", flush=True)
        return False

    # 5. Dry run
    if config.dry_run:
        from api import get_token_price, get_token_price_from_event
        if config.mode == "arbitragem" and tokens and len(tokens) == 2 and event:
            price_up = get_token_price(tokens[0], "BUY") or get_token_price_from_event(event, "up")
            price_down = get_token_price(tokens[1], "BUY") or get_token_price_from_event(event, "down")
            if price_up is not None and price_down is not None and price_up <= MAX_TOKEN_PRICE and price_down <= MAX_TOKEN_PRICE:
                total_price = price_up + price_down
                if total_price <= (1.0 - ARB_MIN_PROFIT_PCT):
                    shares_arb = bet_size / total_price
                    bet_pct_arb = (bet_size / config.bankroll) * 100 if config.bankroll else 0
                    print(f"  [{market.upper()}] DRY RUN ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} | {shares_arb:.2f} shares | aposta: {bet_pct_arb:.1f}% da banca", flush=True)
                    return True
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
            print(f"  [{market.upper()}] Token @ ${token_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
            return False
        shares = bet_size / token_price
        if config.mode == "arbitragem" and tokens and len(tokens) == 2:
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
                        return True
                    time.sleep(ARB_POLL_INTERVAL)
        if config.mode == "only_hedge_plus" and final_result is not None:
            p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
            dynamic_margin = _dynamic_ev_margin(final_result)
            if p_win <= token_price + dynamic_margin:
                print(f"  [{market.upper()}] EV+ não mais válido: P(win)={p_win:.1%} <= preço ${token_price:.2f}+margem {dynamic_margin:.0%}, pulando.", flush=True)
                return False
        bet_pct = (bet_size / config.bankroll) * 100 if config.bankroll else 0
        ev_edge = ""
        if config.mode == "only_hedge_plus" and final_result is not None:
            p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
            edge_pct = (p_win - token_price) * 100
            ev_edge = f" | EV+ edge: {edge_pct:.1f}%"
        print(f"  [{market.upper()}] DRY RUN: {trade_direction.upper()} @ ${token_price:.2f}, {shares:.2f} shares | aposta: {bet_pct:.1f}% da banca{ev_edge}", flush=True)
        if config.mode in ("safe", "only_hedge_plus", "aggressive"):
            _last_bet_window_by_market[market] = window_ts
        return True

    if not tokens:
        print(f"  [{market.upper()}] Mercado não encontrado na Polymarket.", flush=True)
        return False

    from api import get_token_price
    token_id = tokens[0] if trade_direction == "up" else tokens[1]
    real_price = get_token_price(token_id)
    if real_price is not None and real_price > MAX_TOKEN_PRICE:
        print(f"  [{market.upper()}] Token @ ${real_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
        return False
    if config.mode == "only_hedge_plus" and final_result is not None and real_price is not None:
        p_win = final_result.estimated_p_up if trade_direction == "up" else (1 - final_result.estimated_p_up)
        dynamic_margin = _dynamic_ev_margin(final_result)
        if p_win <= real_price + dynamic_margin:
            print(f"  [{market.upper()}] EV+ não mais válido: P(win)={p_win:.1%} <= preço ${real_price:.2f}+margem {dynamic_margin:.0%}, pulando.", flush=True)
            return False

    # 6. Executar ordem(s)
    client = create_clob_client()
    # Sync agressivo para proxy/safe: API pode precisar de múltiplas chamadas para reconhecer allowance
    for _ in range(2):
        _sync_balance_allowance(client)
        time.sleep(1)
    ok = False
    arb_first_one_fill = False
    arb_shares = None

    if config.mode == "arbitragem" and tokens and len(tokens) == 2:
        price_up = get_token_price(tokens[0], "BUY")
        price_down = get_token_price(tokens[1], "BUY")
        if (price_up is not None and price_down is not None
                and price_up <= MAX_TOKEN_PRICE and price_down <= MAX_TOKEN_PRICE
                and (price_up + price_down) <= (1.0 - ARB_MIN_PROFIT_PCT)):
            amount_up = bet_size * price_up / (price_up + price_down)
            amount_down = bet_size * price_down / (price_up + price_down)
            ok1 = place_fok_order(client, tokens[0], amount_up) or place_limit_order(client, tokens[0], amount_up)
            ok2 = place_fok_order(client, tokens[1], amount_down) or place_limit_order(client, tokens[1], amount_down)
            if ok1 and ok2:
                print(f"  [{market.upper()}] ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} | executado", flush=True)
                return True
            if ok1 and not ok2:
                ok = True
                real_price = price_up
                token_id = tokens[0]
                arb_first_one_fill = True
                arb_shares = bet_size / (price_up + price_down)
                trade_direction = "up"

    if not ok:
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
        if config.mode == "arbitragem" and real_price is not None and tokens and len(tokens) == 2:
            other_token_id = tokens[1] if trade_direction == "up" else tokens[0]
            buy_price = real_price
            shares_arb = arb_shares if arb_first_one_fill and arb_shares is not None else bet_size / buy_price
            max_other_price = (1.0 - ARB_MIN_PROFIT_PCT) - buy_price
            if max_other_price > 0:
                while int(time.time()) < close_time - ARB_DEADLINE_T:
                    other_price = get_token_price(other_token_id, "BUY")
                    if other_price is not None and other_price <= max_other_price:
                        amount_second = shares_arb * other_price
                        ok2 = place_fok_order(client, other_token_id, amount_second)
                        if not ok2:
                            ok2 = place_limit_order(client, other_token_id, amount_second)
                        if ok2:
                            print(f"  [{market.upper()}] ARBITRAGEM: comprado lado oposto @ ${other_price:.2f} | executado", flush=True)
                            return True
                    time.sleep(ARB_POLL_INTERVAL)
    else:
        print(f"  [{market.upper()}] Falha ao executar ordem. (Verifique: allowance, saldo USDC, conexão.)", flush=True)

    if ok and config.mode in ("safe", "only_hedge_plus", "aggressive"):
        _last_bet_window_by_market[market] = window_ts
    return ok


def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Min Up/Down Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simular sem ordens reais")
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen", "arbitragem", "only_hedge_plus"], help="Modo de trading")
    parser.add_argument("--safe-bet", type=float, metavar="USD", help="Modo safe: valor fixo em USD por entrada")
    parser.add_argument("--only-hedge-bet", type=float, metavar="USD", help="Modo only_hedge_plus: valor fixo em USD por entrada")
    parser.add_argument("--arbitragem-pct", type=float, metavar="PCT", help="Modo arbitragem: %% da banca por entrada (ex: 25)")
    parser.add_argument("--once", action="store_true", help="Apenas um ciclo")
    parser.add_argument("--max-trades", type=int, help="Máximo de trades (dry-run)")
    parser.add_argument("--markets", type=str, metavar="LIST", help="Mercados: btc, eth, btc15m (ex: btc,btc15m ou both para btc+eth)")
    args = parser.parse_args()

    config = load_config()
    if args.markets:
        raw = args.markets.strip().lower()
        if raw == "both":
            config.markets = ["btc", "eth"]
        elif "," in raw:
            config.markets = [m.strip() for m in raw.split(",") if m.strip() in ("btc", "eth", "btc15m")]
        elif raw in ("btc", "eth", "btc15m"):
            config.markets = [raw]
        else:
            config.markets = ["btc"]
    config.dry_run = args.dry_run
    if args.mode:
        config.mode = args.mode
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
    if config.mode == "safe" and config.fixed_bet_safe is not None:
        print(f"Entrada fixa (safe): ${config.fixed_bet_safe:.2f}", flush=True)
    if config.mode == "only_hedge_plus":
        print(f"Only Hedge+: entrada fixa ${config.fixed_bet_only_hedge or config.min_bet:.2f} | só entra com EV+", flush=True)
    if config.mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        print(f"Arbitragem: {config.arbitragem_bet_pct * 100:.0f}% da banca (via API) | Sem oportunidade = aposta normal", flush=True)
    if config.mode == "aggressive":
        print(f"Agressivo: {MODES['aggressive']['bet_pct']*100:.0f}% da banca (via API)", flush=True)
    print(f"Mercados: {markets_str}", flush=True)
    print("-" * 50, flush=True)

    trades = 0
    while True:
        try:
            for market in config.markets:
                if run_trade_cycle(config, market):
                    trades += 1
            if config.once:
                break
            if config.max_trades and trades >= config.max_trades:
                print(f"\nMax trades ({config.max_trades}) atingido.", flush=True)
                break
            if config.dry_run and config.bankroll < config.min_bet:
                print("\nBankroll abaixo do mínimo, resetando...", flush=True)
                config.bankroll = config.original_bankroll
            now_ts = int(time.time())
            wait = None
            if "btc" in config.markets or "eth" in config.markets:
                next_5m = ((now_ts // WINDOW_SEC) + 1) * WINDOW_SEC
                start_monitor_5m = next_5m + WINDOW_SEC - MONITOR_START_T
                wait_5m = max(0, start_monitor_5m - now_ts)
                if wait is None or (wait_5m < wait):
                    wait = wait_5m
            if "btc15m" in config.markets:
                next_15m = ((now_ts // WINDOW_SEC_15M) + 1) * WINDOW_SEC_15M
                start_monitor_15m = next_15m + WINDOW_SEC_15M - MONITOR_START_T_15M
                wait_15m = max(0, start_monitor_15m - now_ts)
                if wait is None or (wait_15m < wait):
                    wait = wait_15m
            if wait is None:
                wait = 60
            if wait > 240:
                wait = min(wait, 240)
            if wait > 0:
                print(f"\nPróxima janela em {wait:.0f}s...", flush=True)
                time.sleep(wait)
        except KeyboardInterrupt:
            print("\nEncerrado pelo usuário.", flush=True)
            break
        except Exception as e:
            print(f"Erro: {e}", flush=True)
            import traceback
            traceback.print_exc()
            time.sleep(10)


if __name__ == "__main__":
    main()
