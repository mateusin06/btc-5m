#!/usr/bin/env python3
"""
APIs externas: Binance (preços) e Polymarket Gamma (mercados).
"""

import time
from typing import Optional

import requests

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"


def get_btc_price() -> Optional[float]:
    """Preço atual BTC da Binance."""
    try:
        r = requests.get(f"{BINANCE_TICKER}?symbol=BTCUSDT", timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def get_btc_candles_1m(limit: int = 30) -> list[dict]:
    """Candles 1min BTC da Binance."""
    try:
        r = requests.get(
            BINANCE_KLINE,
            params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return [
            {"t": k[0], "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
            for k in data
        ]
    except Exception:
        return []


def get_window_resolution_binance(window_start_ts: int) -> Optional[bool]:
    """
    Verifica se Up ou Down ganhou via Binance.
    Returns: True = Up wins (close >= open), False = Down wins
    """
    try:
        start_ms = window_start_ts * 1000
        end_ms = start_ms + 5 * 60 * 1000
        r = requests.get(
            BINANCE_KLINE,
            params={"symbol": "BTCUSDT", "interval": "1m", "startTime": start_ms, "endTime": end_ms + 60000, "limit": 2},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if len(data) < 2:
            return None
        open_price = float(data[0][1])
        close_price = float(data[1][4])
        return close_price >= open_price
    except Exception:
        return None


def get_window_resolution_polymarket(slug: str) -> Optional[bool]:
    """
    Verifica resolução na Polymarket (Gamma API).
    Polymarket usa Chainlink para resolver; assim o resultado bate com o que aparece no site.
    Returns: True = Up wins, False = Down wins, None = ainda não resolvido ou erro.
    """
    import json
    try:
        event = get_market_by_slug(slug)
        if not event:
            return None
        markets = event.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        outcomes_raw = m.get("outcomes")
        prices_raw = m.get("outcomePrices")
        if not outcomes_raw or not prices_raw:
            return None
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if len(outcomes) < 2 or len(prices) < 2:
            return None
        # Resolvido: um preço é "1" (ou 1) e o outro "0" (ou 0)
        # Determinar o vencedor pelo índice que tem preço >= 0.99 (não depender da ordem Up/Down)
        winner_idx = None
        for i, p in enumerate(prices):
            try:
                if float(p) >= 0.99:
                    winner_idx = i
                    break
            except (TypeError, ValueError):
                pass
        if winner_idx is None:
            # Nenhum preço em 1 → ainda não resolvido
            return None
        winner_label = str(outcomes[winner_idx] if winner_idx < len(outcomes) else "").lower()
        if winner_label == "up":
            return True
        if winner_label == "down":
            return False
        # Fallback: mapeamento por índice como antes (outcomes [Up, Down] → preços na mesma ordem)
        up_idx = None
        down_idx = None
        for i, o in enumerate(outcomes):
            if str(o).lower() == "up":
                up_idx = i
            elif str(o).lower() == "down":
                down_idx = i
        if up_idx is not None and down_idx is not None and winner_idx in (up_idx, down_idx):
            return winner_idx == up_idx
        return None
    except Exception:
        return None


def get_price_to_beat(slug: str) -> Optional[float]:
    """
    Preço de abertura da janela (Price to Beat) da Polymarket/Chainlink.
    Para a *próxima* janela, esse valor é o preço de *fechamento* da janela anterior.
    Returns: preço float ou None se ainda não disponível/erro.
    """
    try:
        event = get_market_by_slug(slug)
        if not event:
            return None
        meta = event.get("eventMetadata") or {}
        ptb = meta.get("priceToBeat")
        if ptb is None:
            return None
        return float(ptb)
    except (TypeError, ValueError, KeyError):
        return None


def get_market_by_slug(slug: str) -> Optional[dict]:
    """
    Busca evento/market na Gamma API pelo slug.
    Returns: dict com markets, token_ids, etc ou None
    """
    try:
        r = requests.get(GAMMA_EVENTS, params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict):
            return data
        return None
    except Exception:
        return None


def get_token_price(token_id: str, side: str = "BUY") -> Optional[float]:
    """Preço do token na Polymarket (BUY = ask, SELL = bid). Retorna valor 0.01-0.99."""
    try:
        from py_clob_client.client import ClobClient
        client = ClobClient("https://clob.polymarket.com")
        raw = client.get_price(token_id, side=side)
        if raw is None:
            return None
        # CLOB retorna {"price": "0.55"} ou às vezes o valor direto
        if isinstance(raw, dict):
            raw = raw.get("price")
        if raw is None:
            return None
        return float(raw)
    except Exception:
        return None


def get_token_price_from_event(event: dict, direction: str) -> Optional[float]:
    """
    Preço do outcome (Up/Down) a partir do evento Gamma (outcomePrices).
    direction: "up" ou "down". Retorna 0.01-0.99 ou None se mercado já resolvido/inválido.
    """
    import json
    if not event:
        return None
    markets = event.get("markets") or []
    if not markets:
        return None
    m = markets[0]
    outcomes_raw = m.get("outcomes")
    prices_raw = m.get("outcomePrices")
    if not outcomes_raw or not prices_raw:
        return None
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    direction_lower = str(direction).lower()
    for i, o in enumerate(outcomes):
        if str(o).lower() == direction_lower and i < len(prices):
            try:
                p = float(prices[i])
                if 0 < p < 1:  # mercado ainda ativo (não resolvido em 0 ou 1)
                    return p
                return None  # resolvido, não usar como preço de entrada
            except (TypeError, ValueError):
                return None
    return None


def extract_token_ids(event: dict) -> Optional[tuple[str, str]]:
    """
    Extrai (token_up, token_down) do evento.
    Polymarket retorna markets com outcomes. outcomes/clobTokenIds são JSON strings.
    """
    import json

    markets = event.get("markets") or []
    if not markets:
        return None

    token_up = None
    token_down = None

    for m in markets:
        # tokens array: [{token_id, outcome}, ...]
        tokens_arr = m.get("tokens") or []
        for t in tokens_arr:
            outcome = (t.get("outcome") or "").lower()
            tid = t.get("token_id")
            if outcome in ("up", "yes"):
                token_up = tid
            elif outcome in ("down", "no"):
                token_down = tid

        if token_up and token_down:
            return (token_up, token_down)

        # Fallback: clobTokenIds + outcomes (JSON strings)
        clob_ids_raw = m.get("clobTokenIds")
        outcomes_raw = m.get("outcomes")
        if clob_ids_raw and outcomes_raw:
            try:
                ids = json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else clob_ids_raw
                outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                if len(ids) >= 2 and len(outcomes) >= 2:
                    for i, o in enumerate(outcomes):
                        o_lower = str(o).lower()
                        if o_lower in ("up", "yes"):
                            token_up = ids[i]
                        elif o_lower in ("down", "no"):
                            token_down = ids[i]
                    if token_up and token_down:
                        return (token_up, token_down)
            except (json.JSONDecodeError, TypeError):
                pass

    # Último fallback: ordem [0]=Up/Yes, [1]=Down/No (Gamma btc-updown usa ["Up","Down"])
    if markets and markets[0].get("clobTokenIds"):
        ids_raw = markets[0]["clobTokenIds"]
        try:
            ids = json.loads(ids_raw) if isinstance(ids_raw, str) else ids_raw
            if len(ids) >= 2:
                return (ids[0], ids[1])
        except (json.JSONDecodeError, TypeError):
            pass
    return None
