import base64
import datetime
from typing import Any, Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def _normalize_pem(pem_text: str) -> str:
    s = (pem_text or "").strip().strip('"').strip("'")
    s = s.replace("\\n", "\n")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s


def _load_private_key(pem_text: str):
    normalized = _normalize_pem(pem_text)
    key_bytes = normalized.encode("utf-8")
    return serialization.load_pem_private_key(
        key_bytes,
        password=None,
        backend=default_backend(),
    )


def _sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    path_without_query = path.split("?")[0]
    message = f"{timestamp_ms}{method}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def _build_headers(api_key_id: str, private_key_pem: str, method: str, path: str) -> dict[str, str]:
    ts = str(int(datetime.datetime.now().timestamp() * 1000))
    private_key = _load_private_key(private_key_pem)
    sign_path = urlparse(KALSHI_BASE_URL + path).path
    signature = _sign_request(private_key, ts, method, sign_path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": ts,
    }


def kalshi_get(api_key_id: str, private_key_pem: str, path: str, params: Optional[dict[str, Any]] = None) -> dict:
    headers = _build_headers(api_key_id, private_key_pem, "GET", path)
    r = requests.get(KALSHI_BASE_URL + path, headers=headers, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def kalshi_post(api_key_id: str, private_key_pem: str, path: str, payload: dict) -> dict:
    headers = _build_headers(api_key_id, private_key_pem, "POST", path)
    headers["Content-Type"] = "application/json"
    r = requests.post(KALSHI_BASE_URL + path, headers=headers, json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def get_balance(api_key_id: str, private_key_pem: str) -> float:
    data = kalshi_get(api_key_id, private_key_pem, "/portfolio/balance")
    balance_cents = data.get("balance")
    return float(balance_cents or 0) / 100.0


def get_markets(
    api_key_id: str,
    private_key_pem: str,
    status: Optional[str] = "open",
    limit: int = 200,
    cursor: str = "",
    series_ticker: str | None = None,
    min_close_ts: int | None = None,
    max_close_ts: int | None = None,
    tickers: str | None = None,
) -> dict:
    params = {"limit": limit}
    if status:
        params["status"] = status
    if cursor:
        params["cursor"] = cursor
    if series_ticker:
        params["series_ticker"] = series_ticker
    if min_close_ts is not None:
        params["min_close_ts"] = int(min_close_ts)
    if max_close_ts is not None:
        params["max_close_ts"] = int(max_close_ts)
    if tickers:
        params["tickers"] = tickers
    return kalshi_get(api_key_id, private_key_pem, "/markets", params=params)


def get_market(api_key_id: str, private_key_pem: str, ticker: str) -> dict:
    return kalshi_get(api_key_id, private_key_pem, f"/markets/{ticker}")


def get_orderbook(api_key_id: str, private_key_pem: str, ticker: str, depth: int = 1) -> dict:
    params = {"depth": depth} if depth is not None else None
    return kalshi_get(api_key_id, private_key_pem, f"/markets/{ticker}/orderbook", params=params)


def create_order(
    api_key_id: str,
    private_key_pem: str,
    ticker: str,
    side: str,
    count: int,
    price_dollars: float,
    time_in_force: str = "fill_or_kill",
) -> dict:
    price_str = f"{price_dollars:.4f}"
    payload = {
        "ticker": ticker,
        "side": side,
        "action": "buy",
        "count": int(count),
        "time_in_force": time_in_force,
    }
    if side == "yes":
        payload["yes_price_dollars"] = price_str
    else:
        payload["no_price_dollars"] = price_str
    return kalshi_post(api_key_id, private_key_pem, "/portfolio/orders", payload)
