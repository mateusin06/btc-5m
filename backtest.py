#!/usr/bin/env python3
"""
Fetcher de candles históricos da Binance para backtesting.
"""

import time
from typing import Optional

import requests

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"


def fetch_candles(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: int = 1000,
) -> list[dict]:
    """
    Busca candles da Binance.

    Returns:
        Lista de dicts com keys: t (timestamp), o, h, l, c, v
    """
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ts:
        params["startTime"] = start_ts
    if end_ts:
        params["endTime"] = end_ts

    resp = requests.get(BINANCE_KLINE_URL, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    candles = []
    for k in data:
        candles.append({
            "t": k[0],
            "o": float(k[1]),
            "h": float(k[2]),
            "l": float(k[3]),
            "c": float(k[4]),
            "v": float(k[5]),
        })
    return candles


def fetch_candles_range(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    start_ts: int,
    end_ts: int,
) -> list[dict]:
    """
    Busca todos os candles em um intervalo (Binance limita 1000 por request).
    """
    all_candles = []
    current = start_ts
    while current < end_ts:
        batch = fetch_candles(
            symbol=symbol,
            interval=interval,
            start_ts=current,
            end_ts=end_ts,
            limit=1000,
        )
        if not batch:
            break
        all_candles.extend(batch)
        current = batch[-1]["t"] + 60_000  # 1min em ms
        time.sleep(0.2)  # rate limit
    return all_candles


def get_candles_for_window(
    candles: list[dict],
    window_start_ts: int,
    minutes_before: int = 25,
) -> list[dict]:
    """
    Extrai candles 1min relevantes para uma janela 5min.
    Inclui `minutes_before` candles antes do window_start para contexto.
    """
    window_ms = window_start_ts * 1000
    start_ms = window_ms - minutes_before * 60 * 1000
    end_ms = window_ms + 6 * 60 * 1000  # janela + 1 extra

    return [c for c in candles if start_ms <= c["t"] <= end_ms]
