#!/usr/bin/env python3
"""
Polymarket BTC 5-Min Up/Down Trading Bot.

Engine principal: timing baseado em relógio, loop de TA, execução de ordens.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Configurações de modos
MODES = {
    "safe": {"min_confidence": 0.50},  # aposta fixa em $ (perguntada no início); entrada só com confiança ≥50%
    "aggressive": {"bet_pct": float(os.getenv("AGGRESSIVE_BET_PCT", "25")) / 100.0, "min_confidence": 0.20},
    "degen": {"bet_pct": 1.0, "min_confidence": 0.0},
    "arbitragem": {"min_confidence": 0.30},  # bet_pct definido no início pelo usuário
}
ARB_MIN_PROFIT_PCT = float(os.getenv("ARB_MIN_PROFIT_PCT", "0.04"))  # lucro mínimo (ex: 0.04 = 4%); aceitar 2–3% encontra mais arbs
ARB_POLL_INTERVAL = 1  # verificar preço do outro lado a cada 1s (mais chances de pegar oportunidade)
ARB_DEADLINE_T = 10  # para de tentar hedge N s antes do fechamento

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
WINDOW_SEC = 300
MONITOR_START_T = 180  # começa a monitorar 3 min antes do fechamento (opera só se faltar 3 min ou menos)
HARD_DEADLINE_T = 5
TA_POLL_INTERVAL = 2
SPIKE_THRESHOLD = 1.5
ORDER_RETRY_INTERVAL = 3
MIN_SHARES = 5
LIMIT_FALLBACK_PRICE = 0.95
MAX_TOKEN_PRICE = float(os.getenv("MAX_TOKEN_PRICE", "0.98"))  # 90c default


@dataclass
class Config:
    dry_run: bool
    mode: str
    once: bool
    max_trades: Optional[int]
    bankroll: float
    min_bet: float
    original_bankroll: float
    fixed_bet_safe: Optional[float] = None  # valor fixo em $ no modo safe (perguntado no início)
    arbitragem_bet_pct: Optional[float] = None  # % da banca no modo arbitragem (perguntado no início)


def delta_to_token_price(delta_pct: float) -> float:
    """Modelo de preço do token baseado no delta (para dry-run/backtest)."""
    abs_d = abs(delta_pct)
    if abs_d < 0.005:
        return 0.50
    if abs_d < 0.02:
        return 0.50 + (abs_d - 0.005) / 0.015 * 0.05  # ~0.50-0.55
    if abs_d < 0.05:
        return 0.55 + (abs_d - 0.02) / 0.03 * 0.10  # ~0.55-0.65
    if abs_d < 0.10:
        return 0.65 + (abs_d - 0.05) / 0.05 * 0.15  # ~0.65-0.80
    if abs_d < 0.15:
        return 0.80 + (abs_d - 0.10) / 0.05 * 0.12  # ~0.80-0.92
    return min(0.92 + (abs_d - 0.15) * 0.5, 0.97)


def get_window_ts() -> int:
    """Timestamp de início da janela 5min atual (divisível por 300)."""
    return int(time.time()) // WINDOW_SEC * WINDOW_SEC


def seconds_until_next_window() -> float:
    """Segundos até 1 segundo após o início da próxima janela (para não pular nenhuma)."""
    now = time.time()
    current_start = int(now) // WINDOW_SEC * WINDOW_SEC
    next_start = current_start + WINDOW_SEC
    return (next_start + 1) - now


def seconds_until_close() -> int:
    """Segundos até o fechamento da janela atual."""
    return (get_window_ts() + WINDOW_SEC) - int(time.time())


def load_config() -> Config:
    bankroll = float(os.getenv("STARTING_BANKROLL", "10.0"))
    min_bet = float(os.getenv("MIN_BET", "5.0"))
    # MAX_TOKEN_PRICE pode ser sobrescrito via .env (ex: 0.90 = 90c)
    return Config(
        dry_run=False,
        mode=os.getenv("BOT_MODE", "safe"),
        once=False,
        max_trades=None,
        bankroll=bankroll,
        min_bet=min_bet,
        original_bankroll=bankroll,
    )


TRADES_LOG = Path(__file__).resolve().parent / "data" / (
    f"trades_{re.sub(r'[^a-zA-Z0-9\-]', '', (os.getenv('BOT_USER_ID') or 'default')[:64]) or 'default'}.jsonl"
)


def log_trade(
    config: Config,
    slug: str,
    direction: str,
    result: Optional[str],
    pnl: Optional[float],
    bet_size: float,
) -> None:
    """Append one trade record to data/trades.jsonl for dashboard stats."""
    try:
        TRADES_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "mode": config.mode,
            "dry_run": config.dry_run,
            "direction": direction,
            "result": result,  # "win" | "loss" | "arb" | null (order placed, outcome unknown)
            "pnl": round(pnl, 2) if pnl is not None else None,
            "bankroll_after": round(config.bankroll, 2) if config.bankroll else None,
            "slug": slug,
            "bet_size": round(bet_size, 2),
        }
        with open(TRADES_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def create_clob_client():
    """Cria ClobClient autenticado."""
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

    api_key = os.getenv("POLY_API_KEY", "").strip()
    api_secret = os.getenv("POLY_API_SECRET", "").strip()
    api_pass = os.getenv("POLY_API_PASSPHRASE", "").strip()

    if not key or key == "0x...":
        raise ValueError("Defina POLY_PRIVATE_KEY no .env")

    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        signature_type=sig_type,
        funder=funder if funder else None,
    )

    if api_key and api_secret and api_pass:
        client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass))
    else:
        client.set_api_creds(client.create_or_derive_api_creds())

    return client


def place_fok_order(client, token_id: str, amount_usd: float) -> bool:
    """FOK market buy. Retorna True se preenchido."""
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    mo = MarketOrderArgs(
        token_id=token_id,
        amount=amount_usd,
        side=BUY,
        price=MAX_TOKEN_PRICE,  # slippage: não paga mais que 90c
        order_type=OrderType.FOK,
    )
    try:
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        return resp.get("status") in ("matched", "live")

    except Exception as e:
        print(f"  FOK falhou: {e}")
        return False


def place_limit_order(client, token_id: str, amount_usd: float) -> bool:
    """GTC limit buy a 90c como fallback."""
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
        print(f"  Limit fallback falhou: {e}")
        return False


def place_sell_order(client, token_id: str, shares: float, min_price: float) -> bool:
    """Vende posição (limit sell no mínimo min_price). Retorna True se executado."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import SELL

    if shares < MIN_SHARES:
        return False
    try:
        order = OrderArgs(token_id=token_id, price=min_price, size=shares, side=SELL)
        signed = client.create_order(order)
        resp = client.post_order(signed, OrderType.GTC)
        if resp.get("status") in ("live", "matched"):
            return True
        # Tentar FOK market sell
        from py_clob_client.clob_types import MarketOrderArgs
        mo = MarketOrderArgs(
            token_id=token_id,
            amount=shares,
            side=SELL,
            price=min_price,
            order_type=OrderType.FOK,
        )
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        return resp.get("status") == "matched"
    except Exception as e:
        print(f"  Venda falhou: {e}", flush=True)
        return False


def run_trade_cycle(config: Config) -> bool:
    """Executa um ciclo completo: espera, analisa, opera, resolve."""
    from api import get_btc_price, get_btc_candles_1m, get_market_by_slug, extract_token_ids, get_window_resolution_binance
    from strategy import analyze

    window_ts = get_window_ts()
    slug = f"btc-updown-5m-{window_ts}"
    close_time = window_ts + WINDOW_SEC

    # 1. Esperar até entrar na janela (arbitragem: desde o início; outros: últimos 3 min)
    monitor_secs = WINDOW_SEC if config.mode == "arbitragem" else MONITOR_START_T
    while True:
        secs = seconds_until_close()
        if secs <= monitor_secs:
            break
        if secs % 60 == 0 or secs <= 30:
            print(f"  Janela fecha em {secs}s... (slug: {slug})", flush=True)
        time.sleep(1)

    # 2. Buscar preço de abertura (candle 1min no início da janela)
    candles = get_btc_candles_1m(limit=15)
    window_open = None
    for c in candles:
        candle_start_sec = c["t"] // 1000  # Binance retorna t em ms
        if candle_start_sec == window_ts:
            window_open = c["o"]
            break
    if window_open is None:
        window_open = candles[0]["o"] if candles else get_btc_price() or 0

    if not window_open:
        print("  ERRO: Não foi possível obter preço de abertura.", flush=True)
        return False

    # 3. Loop TA até T-5s (dispara assim que spike ou confiança atingir)
    tick_prices = []
    best_score = 0.0
    best_result = None
    prev_score = 0.0
    fired = False
    trade_direction = None
    final_result = None

    while int(time.time()) < close_time - HARD_DEADLINE_T:
        price = get_btc_price()
        if price:
            tick_prices.append(price)
        candles = get_btc_candles_1m(limit=30)
        if not candles:
            time.sleep(TA_POLL_INTERVAL)
            continue

        result = analyze(window_open, price or candles[-1]["c"], candles, tick_prices[-20:] if tick_prices else None)

        if abs(result.score) > abs(best_score):
            best_score = result.score
            best_result = result

        # Spike detection (comparar com iteração anterior)
        if abs(result.score - prev_score) >= SPIKE_THRESHOLD and prev_score != 0:
            trade_direction = result.direction
            final_result = result
            fired = True
            print(f"  SPIKE! Score {result.score:.2f} -> {result.direction}", flush=True)
            break

        mode_cfg = MODES.get(config.mode, MODES["safe"])
        if result.confidence >= mode_cfg["min_confidence"]:
            trade_direction = result.direction
            final_result = result
            fired = True
            print(f"  Confiança {result.confidence:.1%} -> {result.direction}", flush=True)
            break

        prev_score = result.score
        time.sleep(TA_POLL_INTERVAL)

    # T-5s: usar melhor sinal se ainda não disparou
    if not fired and best_result:
        trade_direction = best_result.direction
        final_result = best_result
        fired = True
        print(f"  T-5s: melhor sinal -> {trade_direction} (score {best_score:.2f})", flush=True)

    if not fired or not trade_direction:
        print("  Sem sinal válido, pulando janela.", flush=True)
        return False

    # 4. Calcular tamanho da aposta
    mode_cfg = MODES.get(config.mode, MODES["safe"])
    if config.mode == "safe" and config.fixed_bet_safe is not None:
        bet_size = config.fixed_bet_safe
    elif config.mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        bet_size = config.bankroll * config.arbitragem_bet_pct
    elif config.mode == "aggressive" and config.bankroll > config.original_bankroll:
        bet_size = config.bankroll - config.original_bankroll
    else:
        bet_size = config.bankroll * mode_cfg.get("bet_pct", 0.25)

    bet_size = max(bet_size, config.min_bet)
    if bet_size > config.bankroll:
        bet_size = config.bankroll

    if bet_size < config.min_bet:
        print(f"  Bankroll insuficiente: ${config.bankroll:.2f} < min ${config.min_bet:.2f}")
        return False

    # 5. Buscar mercado e token IDs
    event = get_market_by_slug(slug)
    tokens = extract_token_ids(event) if event else None

    if config.dry_run:
        # Dry run: preço real da Polymarket quando disponível, senão modelo
        from api import get_token_price, get_token_price_from_event, get_window_resolution_polymarket, get_window_resolution_binance
        # Modo arbitragem: priorizar arb "pura" (ambos os lados já baratos) antes de aposta direcional
        if config.mode == "arbitragem" and tokens and len(tokens) == 2 and event:
            price_up = get_token_price(tokens[0], "BUY") or get_token_price_from_event(event, "up")
            price_down = get_token_price(tokens[1], "BUY") or get_token_price_from_event(event, "down")
            if price_up is not None and price_down is not None and price_up <= MAX_TOKEN_PRICE and price_down <= MAX_TOKEN_PRICE:
                total_price = price_up + price_down
                if total_price <= (1.0 - ARB_MIN_PROFIT_PCT):
                    shares_arb = bet_size / total_price
                    payout_arb = shares_arb * 1.0
                    pnl_arb = payout_arb - bet_size
                    pct_arb = (pnl_arb / bet_size) * 100
                    config.bankroll += pnl_arb
                    print(f"  DRY RUN ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} = ${total_price:.2f} | {shares_arb:.2f} shares", flush=True)
                    print(f"  ARBITRAGEM: lucro garantido {pct_arb:.1f}% | PnL ${pnl_arb:+.2f} | Bankroll ${config.bankroll:.2f}", flush=True)
                    log_trade(config, slug, "arb", "arb", pnl_arb, bet_size)
                    return True
        token_id_dry = tokens[0] if trade_direction == "up" else tokens[1] if tokens else None
        token_price = None
        if token_id_dry:
            token_price = get_token_price(token_id_dry, "BUY")
            # CLOB pode demorar alguns segundos para ter livro da janela; retentar para preferir preço real
            if token_price is None and int(time.time()) < close_time:
                for _ in range(5):
                    time.sleep(2)
                    token_price = get_token_price(token_id_dry, "BUY")
                    if token_price is not None:
                        break
        # Fallback: preço do evento Gamma (outcomePrices) = preço real Polymarket
        if token_price is None and event:
            token_price = get_token_price_from_event(event, trade_direction)
        price_source = "real" if token_price is not None else None
        if token_price is None:
            token_price = delta_to_token_price(final_result.window_delta_pct if final_result else 0)
            price_source = "estimado"
        if token_price > MAX_TOKEN_PRICE:
            print(f"  Token @ ${token_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
            return False
        shares = bet_size / token_price
        # Modo arbitragem: comprar lado oposto quando preço permitir lucro garantido 4%
        if config.mode == "arbitragem" and tokens and len(tokens) == 2:
            token_other_id = tokens[1] if trade_direction == "up" else tokens[0]
            max_other_price = (1.0 - ARB_MIN_PROFIT_PCT) - token_price  # 0.96 - P_our para 4% lucro
            if max_other_price > 0:
                while int(time.time()) < close_time - ARB_DEADLINE_T:
                    other_price = get_token_price(token_other_id, "BUY")
                    if other_price is not None and other_price <= max_other_price:
                        cost_second = shares * other_price
                        total_cost = bet_size + cost_second
                        payout = shares * 1.0  # um dos dois paga $1
                        pnl_arb = payout - total_cost
                        config.bankroll += pnl_arb
                        pct_arb = (pnl_arb / total_cost) * 100
                        print(f"  DRY RUN: {trade_direction.upper()} @ ${token_price:.2f} ({price_source}), {shares:.2f} shares", flush=True)
                        print(f"  ARBITRAGEM: comprado lado oposto @ ${other_price:.2f} (lucro garantido {pct_arb:.1f}%) | PnL ${pnl_arb:+.2f} | Bankroll ${config.bankroll:.2f}", flush=True)
                        log_trade(config, slug, trade_direction, "arb", pnl_arb, bet_size + cost_second)
                        return True
                    time.sleep(ARB_POLL_INTERVAL)
        # Sem oportunidade de arbitragem nesta janela → aposta segue normal, aguardar resolução
        print("  Sem oportunidade de arbitragem nesta janela; aposta executada normalmente.", flush=True)
        time.sleep(max(0, close_time - int(time.time()) + 2))
        print(f"  DRY RUN: {trade_direction.upper()} @ ${token_price:.2f} ({price_source}), {shares:.2f} shares", flush=True)
        # Resolução: preferir Chainlink (Price to Beat) para bater com Polymarket
        from api import get_price_to_beat, get_window_resolution_polymarket, get_window_resolution_binance
        next_window_ts = window_ts + WINDOW_SEC
        slug_next = f"btc-updown-5m-{next_window_ts}"
        up_wins = None
        for _ in range(60):  # até 2 min para a Polymarket/Chainlink preencher eventMetadata.priceToBeat
            open_chainlink = get_price_to_beat(slug)      # abertura da nossa janela (Chainlink)
            close_chainlink = get_price_to_beat(slug_next) # abertura da próxima = fechamento da nossa (Chainlink)
            if open_chainlink is not None and close_chainlink is not None:
                up_wins = close_chainlink >= open_chainlink  # mesma regra Polymarket
                break
            if close_chainlink is not None:
                # só temos fechamento; usar abertura Binance (pode divergir levemente da Chainlink)
                up_wins = close_chainlink >= window_open
                break
            time.sleep(2)
        if up_wins is None:
            up_wins = get_window_resolution_polymarket(slug)
        if up_wins is None:
            up_wins = get_window_resolution_binance(window_ts)
            if up_wins is not None:
                print("  (resolução via Binance; Price to Beat ainda não disponível)", flush=True)
        if up_wins is None:
            print("  Não foi possível verificar resolução.", flush=True)
            return False
        won = (trade_direction == "up" and up_wins) or (trade_direction == "down" and not up_wins)
        pnl = (shares * 1.0 - bet_size) if won else -bet_size
        config.bankroll += pnl
        print(f"  Resultado ({slug}): {'UP' if up_wins else 'DOWN'} -> {'WIN' if won else 'LOSS'} | PnL ${pnl:+.2f} | Bankroll ${config.bankroll:.2f}", flush=True)
        log_trade(config, slug, trade_direction, "win" if won else "loss", pnl, bet_size)
        return True

    if not tokens:
        print("  Mercado não encontrado na Polymarket.", flush=True)
        return False

    # Verificar preço real na Polymarket (max 90c)
    from api import get_token_price
    token_id = tokens[0] if trade_direction == "up" else tokens[1]
    real_price = get_token_price(token_id)
    if real_price is not None and real_price > MAX_TOKEN_PRICE:
        print(f"  Token @ ${real_price:.2f} > 90c, pulando (max ${MAX_TOKEN_PRICE:.2f})", flush=True)
        return False

    # 6. Executar ordem(s)
    client = create_clob_client()
    ok = False
    arb_first_one_fill = False  # True se fizemos arb pura mas só a 1ª ordem encheu
    arb_shares = None  # shares quando arb pura com só 1ª ordem (para hedge)

    # Modo arbitragem: tentar arb pura (ambos os lados) antes de aposta direcional
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
                pct_arb = (1.0 - (price_up + price_down)) / (price_up + price_down) * 100
                print(f"  ARB PURA: Up @ ${price_up:.2f} + Down @ ${price_down:.2f} | lucro garantido ~{pct_arb:.1f}%", flush=True)
                log_trade(config, slug, "arb", "placed", None, bet_size)
                return True
            if ok1 and not ok2:
                ok = True
                real_price = price_up
                token_id = tokens[0]
                arb_first_one_fill = True
                arb_shares = bet_size / (price_up + price_down)
                trade_direction = "up"  # já compramos Up, queremos hedge em Down

    if not ok:
        while int(time.time()) < close_time and not ok:
            ok = place_fok_order(client, token_id, bet_size)
            if not ok:
                time.sleep(ORDER_RETRY_INTERVAL)
        if not ok:
            ok = place_limit_order(client, token_id, bet_size)

    if ok:
        print(f"  Ordem executada: {trade_direction.upper()} ${bet_size:.2f}", flush=True)
        # Modo arbitragem: comprar lado oposto quando preço permitir lucro garantido
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
                            pct_arb = ((1.0 - (buy_price + other_price)) / (buy_price + other_price)) * 100
                            print(f"  ARBITRAGEM: comprado lado oposto @ ${other_price:.2f} (lucro garantido ~{pct_arb:.1f}%)", flush=True)
                            log_trade(config, slug, trade_direction, "placed", None, bet_size)
                            return True
                    time.sleep(ARB_POLL_INTERVAL)
    else:
        print("  Falha ao executar ordem.", flush=True)

    if ok:
        log_trade(config, slug, trade_direction, "placed", None, bet_size)
    return ok


def main():
    parser = argparse.ArgumentParser(description="Polymarket BTC 5-Min Up/Down Bot")
    parser.add_argument("--dry-run", action="store_true", help="Simular sem ordens reais")
    parser.add_argument("--mode", choices=["safe", "aggressive", "degen", "arbitragem"], help="Modo de trading")
    parser.add_argument("--safe-bet", type=float, metavar="USD", help="Modo safe: valor fixo em USD por entrada (evita pergunta no terminal)")
    parser.add_argument("--arbitragem-pct", type=float, metavar="PCT", help="Modo arbitragem: %% da banca por entrada (ex: 25)")
    parser.add_argument("--once", action="store_true", help="Apenas um ciclo")
    parser.add_argument("--max-trades", type=int, help="Máximo de trades (dry-run)")
    args = parser.parse_args()

    config = load_config()
    config.dry_run = args.dry_run
    if args.mode:
        config.mode = args.mode
    config.once = args.once
    config.max_trades = args.max_trades

    if config.mode == "safe":
        if args.safe_bet is not None:
            v = args.safe_bet
            if v < config.min_bet:
                print(f"Erro: --safe-bet deve ser >= ${config.min_bet:.2f}", flush=True)
                sys.exit(1)
            if v > config.bankroll:
                print(f"Erro: --safe-bet não pode ser maior que o bankroll (${config.bankroll:.2f})", flush=True)
                sys.exit(1)
            config.fixed_bet_safe = v
        elif sys.stdin.isatty() and sys.stderr.isatty():
            while True:
                try:
                    print(f"Modo safe: valor fixo de entrada em USD (ex: 5.00) [min ${config.min_bet:.2f}]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        print("  Informe um valor em dólares.", flush=True, file=sys.stderr)
                        continue
                    v = float(s.replace(",", "."))
                    if v < config.min_bet:
                        print(f"  Valor deve ser >= ${config.min_bet:.2f}", flush=True, file=sys.stderr)
                        continue
                    if v > config.bankroll:
                        print(f"  Valor não pode ser maior que o bankroll (${config.bankroll:.2f})", flush=True, file=sys.stderr)
                        continue
                    config.fixed_bet_safe = v
                    break
                except ValueError:
                    print("  Valor inválido. Use número (ex: 5.00)", flush=True, file=sys.stderr)
        else:
            print("Modo safe exige valor de entrada. Use --safe-bet 5.0 (ex.: python bot.py --dry-run --mode safe --safe-bet 5.0 > resultados.txt 2>&1)", flush=True, file=sys.stderr)
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
                    print("Modo arbitragem: % da banca por entrada (ex: 25 para 25%) [1-100]: ", end="", flush=True, file=sys.stderr)
                    s = input().strip()
                    if not s:
                        print("  Informe um percentual.", flush=True, file=sys.stderr)
                        continue
                    pct = float(s.replace(",", "."))
                    if pct < 1 or pct > 100:
                        print("  Use um valor entre 1 e 100.", flush=True, file=sys.stderr)
                        continue
                    config.arbitragem_bet_pct = pct / 100.0
                    break
                except ValueError:
                    print("  Valor inválido. Use número (ex: 25).", flush=True, file=sys.stderr)
        else:
            print("Modo arbitragem exige %% da banca. Use --arbitragem-pct 25 (ex.: python bot.py --mode arbitragem --arbitragem-pct 25)", flush=True, file=sys.stderr)
            sys.exit(1)
        print("  Aviso: Se não encontrar oportunidade de arbitragem, a aposta será executada normalmente (estratégia simples, sem hedge).", flush=True)

    print(f"Polymarket BTC 5-Min Bot | Modo: {config.mode} | Dry-run: {config.dry_run}", flush=True)
    print(f"Bankroll: ${config.bankroll:.2f} | Min bet: ${config.min_bet:.2f} | Max token: 98c", flush=True)
    if config.mode == "safe" and config.fixed_bet_safe is not None:
        print(f"Entrada fixa (safe): ${config.fixed_bet_safe:.2f}", flush=True)
    if config.mode == "arbitragem" and config.arbitragem_bet_pct is not None:
        print(f"Arbitragem: {config.arbitragem_bet_pct * 100:.0f}% da banca por entrada | Sem oportunidade = aposta normal", flush=True)
    print("-" * 50, flush=True)

    trades = 0
    while True:
        try:
            if run_trade_cycle(config):
                trades += 1
            if config.once:
                break
            if config.max_trades and trades >= config.max_trades:
                print(f"\nMax trades ({config.max_trades}) atingido.", flush=True)
                break
            if config.dry_run and config.bankroll < config.min_bet:
                print("\nBankroll abaixo do mínimo, resetando para coleta de dados...", flush=True)
                config.bankroll = config.original_bankroll
            # Esperar exatamente até 1s após o início da próxima janela (não pula nenhuma)
            # Se acabamos de sair de um ciclo, já estamos na próxima janela; não esperar 288s e pular
            wait = seconds_until_next_window()
            if wait > 240:  # >4min até "próxima" = estamos no início da janela que acabou de abrir
                wait = 0
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
