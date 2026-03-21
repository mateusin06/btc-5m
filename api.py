#!/usr/bin/env python3
"""
APIs externas: Binance (preços) e Polymarket Gamma (mercados).
"""

import json
import os
import time
from typing import Optional

import requests

BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price"
BINANCE_KLINE = "https://api.binance.com/api/v3/klines"
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "").strip() or "https://polygon-bor-rpc.publicnode.com"

# Chainlink Data Feeds (Polygon Mainnet)
CHAINLINK_FEED_POLYGON = {
    "btc": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "eth": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
}
BINANCE_TRADES = "https://api.binance.com/api/v3/trades"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"


def get_rtds_price(symbol: str, timeout: float = 2.5) -> Optional[float]:
    """
    PreÃ§o atual via Polymarket RTDS (Binance source). Ex.: symbol="btcusdt" ou "ethusdt".
    """
    try:
        from websocket import create_connection
    except Exception:
        return None
    ws = None
    try:
        ws = create_connection("wss://ws-live-data.polymarket.com", timeout=timeout)
        ws.settimeout(timeout)
        sub = {
            "action": "subscribe",
            "subscriptions": [
                {"topic": "crypto_prices", "type": "update", "filters": symbol.lower()}
            ],
        }
        ws.send(json.dumps(sub))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = ws.recv()
            except Exception:
                break
            try:
                data = json.loads(msg)
            except Exception:
                continue
            if data.get("topic") != "crypto_prices" or data.get("type") != "update":
                continue
            payload = data.get("payload") or {}
            if (payload.get("symbol") or "").lower() == symbol.lower():
                value = payload.get("value")
                if value is not None:
                    return float(value)
    finally:
        try:
            if ws is not None:
                ws.close()
        except Exception:
            pass
    return None


def get_btc_price() -> Optional[float]:
    """Preço atual BTC da Binance."""
    try:
        r = requests.get(f"{BINANCE_TICKER}?symbol=BTCUSDT", timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def get_eth_price() -> Optional[float]:
    """Preço atual ETH da Binance."""
    try:
        r = requests.get(f"{BINANCE_TICKER}?symbol=ETHUSDT", timeout=5)
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


def get_eth_candles_1m(limit: int = 30) -> list[dict]:
    """Candles 1min ETH da Binance."""
    try:
        r = requests.get(
            BINANCE_KLINE,
            params={"symbol": "ETHUSDT", "interval": "1m", "limit": limit},
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


def get_btc_candles_15m(limit: int = 20) -> list[dict]:
    """Candles 15min BTC da Binance (para mercado btc 15m)."""
    try:
        r = requests.get(
            BINANCE_KLINE,
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": limit},
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


def get_price_by_market(market: str) -> Optional[float]:
    """Preço atual por mercado (btc, eth ou btc15m — btc15m usa preço BTC)."""
    if market in ("eth", "eth15m"):
        return get_eth_price()
    return get_btc_price()


def get_candles_by_market(market: str, limit: int = 30) -> list[dict]:
    """Candles por mercado: 1min para btc/eth, 15min para btc15m."""
    if market == "btc15m":
        return get_btc_candles_15m(limit=min(limit, 20))
    if market == "eth15m":
        return get_eth_candles_1m(limit=limit)
    if market == "eth":
        return get_eth_candles_1m(limit=limit)
    return get_btc_candles_1m(limit=limit)


def get_cvd_by_market(market: str, limit: int = 500) -> float:
    """
    Calcula um snapshot de CVD (Cumulative Volume Delta) a partir dos últimos trades da Binance.

    Usa /api/v3/trades (trades mais recentes). Para cada trade:
    - se isBuyerMaker == True  -> comprador é maker, vendedor é agressor (bate no BID)  -> CVD -= qty
    - se isBuyerMaker == False -> comprador é agressor (bate no ASK)                   -> CVD += qty
    """
    symbol = "BTCUSDT"
    if market == "eth":
        symbol = "ETHUSDT"
    try:
        limit = max(50, min(1000, int(limit)))
        r = requests.get(
            BINANCE_TRADES,
            params={"symbol": symbol, "limit": limit},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        cvd = 0.0
        for t in data:
            try:
                qty = float(t.get("qty") or t.get("quantity") or 0.0)
            except Exception:
                qty = 0.0
            is_buyer_maker = bool(t.get("isBuyerMaker"))
            if is_buyer_maker:
                cvd -= qty
            else:
                cvd += qty
        return cvd
    except Exception:
        return 0.0


def get_cvd_snapshot(market: str, limit: int = 500) -> dict:
    """
    Snapshot de CVD e volume usando trades recentes da Binance.
    Retorna cvd total, volume total e comparação entre metade recente e anterior (momentum).
    """
    symbol = "BTCUSDT"
    if market == "eth":
        symbol = "ETHUSDT"
    try:
        limit = max(50, min(1000, int(limit)))
        r = requests.get(
            BINANCE_TRADES,
            params={"symbol": symbol, "limit": limit},
            timeout=5,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            return {"cvd": 0.0, "total_vol": 0.0, "cvd_recent": 0.0, "vol_recent": 0.0, "cvd_prev": 0.0, "vol_prev": 0.0, "n": 0}

        def _sum_trades(trades: list[dict]) -> tuple[float, float]:
            cvd = 0.0
            vol = 0.0
            for t in trades:
                try:
                    qty = float(t.get("qty") or t.get("quantity") or 0.0)
                except Exception:
                    qty = 0.0
                vol += qty
                is_buyer_maker = bool(t.get("isBuyerMaker"))
                cvd += (-qty if is_buyer_maker else qty)
            return cvd, vol

        mid = len(data) // 2
        cvd_total, vol_total = _sum_trades(data)
        cvd_prev, vol_prev = _sum_trades(data[:mid]) if mid > 0 else (0.0, 0.0)
        cvd_recent, vol_recent = _sum_trades(data[mid:]) if mid > 0 else (0.0, 0.0)
        return {
            "cvd": cvd_total,
            "total_vol": vol_total,
            "cvd_recent": cvd_recent,
            "vol_recent": vol_recent,
            "cvd_prev": cvd_prev,
            "vol_prev": vol_prev,
            "n": len(data),
        }
    except Exception:
        return {"cvd": 0.0, "total_vol": 0.0, "cvd_recent": 0.0, "vol_recent": 0.0, "cvd_prev": 0.0, "vol_prev": 0.0, "n": 0}


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


def _get_price_to_beat_legacy(slug: str) -> Optional[float]:
    """
    Preço de abertura da janela (Price to Beat) da Polymarket/Chainlink.
    É o preço usado na resolução: Up se fechamento Chainlink >= este valor.
    Returns: preço float ou None se ainda não disponível/erro.
    """
    try:
        return None
        meta = event.get("eventMetadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if isinstance(meta, dict):
            for key in ("priceToBeat", "price_to_beat", "priceToBeatUsd", "price_to_beat_usd"):
                if key in meta and meta[key] is not None:
                    return float(meta[key])
        # Fallback: algumas respostas trazem o PTB no market metadata
        markets = event.get("markets") or []
        if markets:
            m0 = markets[0] or {}
            m_meta = m0.get("metadata") or m0.get("marketMetadata") or {}
            if isinstance(m_meta, str):
                try:
                    m_meta = json.loads(m_meta)
                except Exception:
                    m_meta = {}
            if isinstance(m_meta, dict):
                ptb2 = m_meta.get("priceToBeat") or m_meta.get("price_to_beat") or m_meta.get("priceToBeatUsd") or m_meta.get("price_to_beat_usd")
                if ptb2 is not None:
                    return float(ptb2)
            # Extrair de texto (ex.: "Price to beat $69,978.55")
            text_fields = [
                m0.get("subtitle"),
                m0.get("title"),
                m0.get("question"),
                m0.get("description"),
            ]
        else:
            text_fields = []
        # Também tentar no evento
        text_fields += [
            event.get("subtitle"),
            event.get("title"),
            event.get("description"),
        ]
        import re
        for s in text_fields:
            if not s:
                continue
            m = re.search(r"(price to beat|target price)[^0-9]*([0-9][0-9,]*\.?[0-9]*)", str(s), re.IGNORECASE)
            if m:
                try:
                    return float(m.group(2).replace(",", ""))
                except Exception:
                    pass
        # Fallback extra: buscar direto no endpoint /markets
        try:
            r2 = requests.get(GAMMA_MARKETS, params={"slug": slug}, timeout=10)
            if r2.ok:
                data2 = r2.json()
                mkt = None
                if isinstance(data2, list) and data2:
                    mkt = data2[0]
                elif isinstance(data2, dict):
                    mkt = data2
                if mkt:
                    m_meta2 = mkt.get("marketMetadata") or mkt.get("metadata") or {}
                    if isinstance(m_meta2, str):
                        try:
                            m_meta2 = json.loads(m_meta2)
                        except Exception:
                            m_meta2 = {}
                    if isinstance(m_meta2, dict):
                        ptb3 = m_meta2.get("priceToBeat") or m_meta2.get("price_to_beat") or m_meta2.get("priceToBeatUsd") or m_meta2.get("price_to_beat_usd")
                        if ptb3 is not None:
                            return float(ptb3)
        except Exception:
            pass
        return None
    except (TypeError, ValueError, KeyError):
        return None


def get_price_to_beat(slug: str) -> Optional[float]:
    """
    PreÃ§o de abertura da janela (Price to Beat) da Polymarket/Chainlink.
    Ã‰ o preÃ§o usado na resoluÃ§Ã£o: Up se fechamento Chainlink >= este valor.
    Returns: preÃ§o float ou None se ainda nÃ£o disponÃ­vel/erro.
    """
    try:
        # Somente abertura da janela via Binance como PTB
        try:
            window_ts = int(slug.split("-")[-1])
        except Exception:
            return None
        market_key = "eth" if slug.startswith("eth-") else "btc"
        return get_window_open_binance(market_key, window_ts, is_15m=True)
    except (TypeError, ValueError, KeyError):
        return None


def get_window_open_binance(market: str, window_ts: int, is_15m: bool = False) -> Optional[float]:
    """
    Preço de abertura da janela via Binance (primeiro candle da janela).
    Útil para comparar com Chainlink (get_price_to_beat) e obter delta.
    """
    if is_15m:
        symbol = "ETHUSDT" if market == "eth" else "BTCUSDT"
        try:
            r = requests.get(
                BINANCE_KLINE,
                params={"symbol": symbol, "interval": "15m", "startTime": window_ts * 1000, "limit": 1},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data and len(data[0]) >= 2:
                return float(data[0][1])
        except Exception:
            pass
    if market == "eth":
        candles = get_eth_candles_1m(limit=15)
    else:
        candles = get_btc_candles_1m(limit=15) if not is_15m else get_btc_candles_15m(limit=5)
    if not candles:
        return None
    for c in candles:
        candle_start_sec = c["t"] // 1000
        if candle_start_sec == window_ts:
            return float(c["o"])
    return float(candles[0]["o"]) if candles else None


_chainlink_decimals_cache: dict[str, int] = {}


def _rpc_call(rpc_url: str, method: str, params: list) -> Optional[dict]:
    try:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        r = requests.post(rpc_url, json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            return None
        return data
    except Exception:
        return None


def _decode_int256(hex_data: str) -> Optional[int]:
    if not hex_data or not hex_data.startswith("0x"):
        return None
    raw = int(hex_data, 16)
    if raw >= 2 ** 255:
        raw -= 2 ** 256
    return raw


def _decode_uint256(hex_data: str) -> Optional[int]:
    if not hex_data or not hex_data.startswith("0x"):
        return None
    return int(hex_data, 16)


def _get_chainlink_decimals(rpc_url: str, feed_addr: str) -> Optional[int]:
    if feed_addr in _chainlink_decimals_cache:
        return _chainlink_decimals_cache[feed_addr]
    # decimals() selector: 0x313ce567
    data = "0x313ce567"
    resp = _rpc_call(rpc_url, "eth_call", [{"to": feed_addr, "data": data}, "latest"])
    if not resp:
        return None
    result = resp.get("result")
    if not result:
        return None
    dec = _decode_uint256(result)
    if dec is None:
        return None
    _chainlink_decimals_cache[feed_addr] = int(dec)
    return int(dec)


def get_chainlink_latest_price_polygon(market: str) -> Optional[float]:
    """
    Preço atual do feed Chainlink no Polygon (BTC/USD ou ETH/USD).
    """
    if not POLYGON_RPC_URL:
        return None
    feed_addr = CHAINLINK_FEED_POLYGON.get(market)
    if not feed_addr:
        return None
    dec = _get_chainlink_decimals(POLYGON_RPC_URL, feed_addr)
    if dec is None:
        return None
    # latestRoundData() selector: 0x50d25bcd
    data = "0x50d25bcd"
    resp = _rpc_call(POLYGON_RPC_URL, "eth_call", [{"to": feed_addr, "data": data}, "latest"])
    if not resp:
        return None
    result = resp.get("result")
    if not result or not result.startswith("0x") or len(result) < 2 + 64 * 5:
        return None
    # Decode answer (int256) at slot 1 (offset 32 bytes)
    answer_hex = "0x" + result[2 + 64 : 2 + 64 * 2]
    answer = _decode_int256(answer_hex)
    if answer is None:
        return None
    return float(answer) / (10 ** dec)


def get_open_delta_binance_chainlink(slug: str, market: str, window_ts: int, is_15m: bool = False) -> Optional[dict]:
    """
    Delta entre abertura Binance e Chainlink (Price to Beat).
    Resolução é por Chainlink; usar Chainlink como referência alinha a estratégia ao resultado.
    Returns: {"chainlink_open", "binance_open", "delta_usd", "delta_pct"} ou None.
    """
    chainlink_open = get_price_to_beat(slug)
    binance_open = get_window_open_binance(market, window_ts, is_15m)
    if chainlink_open is None or binance_open is None:
        return None
    delta_usd = binance_open - chainlink_open
    delta_pct = (delta_usd / chainlink_open) * 100 if chainlink_open else 0.0
    return {
        "chainlink_open": chainlink_open,
        "binance_open": binance_open,
        "delta_usd": delta_usd,
        "delta_pct": delta_pct,
    }


def get_market_by_slug(slug: str) -> Optional[dict]:
    """
    Busca evento/market na Gamma API pelo slug.
    Returns: dict com markets, token_ids, etc ou None
    """
    try:
        # Gamma API pode servir cache antigo; usar cache-buster + headers no-cache para pegar PTB atual
        headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}
        r = requests.get(
            GAMMA_EVENTS,
            params={"slug": slug, "cb": f"{time.time():.3f}", "cache": f"{time.time():.3f}"},
            headers=headers,
            timeout=10,
        )
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
