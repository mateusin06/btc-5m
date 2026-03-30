#!/usr/bin/env python3
"""
Backend da dashboard do bot Polymarket.
API para config (Supabase), derivar credenciais, start/stop do bot e estatísticas.
Requer login; config e trades são por usuário.
"""

import json
import os
import json
import math
import statistics
import calendar
from zoneinfo import ZoneInfo
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Literal, Optional

import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# Carrega .env apenas se existir (config principal é por variáveis de ambiente)
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

PROJECT_ROOT = Path(__file__).resolve().parent
DATA_DIR = PROJECT_ROOT / "data"
# Em ambiente serverless (ex.: Vercel) o disco pode ser read-only; não criar DATA_DIR aqui
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
TRADES_FILE = DATA_DIR / "trades.jsonl"
ENV_FILE = PROJECT_ROOT / ".env"  # Não usado para leitura/escrita; config é por variáveis de ambiente

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
ADMIN_EMAIL = "acnovogc@gmail.com"
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_ANON_KEY:
    raise RuntimeError("SUPABASE_URL e SUPABASE_ANON_KEY devem ser definidos no ambiente.")

GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
GAMMA_SPORTS = "https://gamma-api.polymarket.com/sports"
GAMMA_SERIES = "https://gamma-api.polymarket.com/series"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
METEOBLUE_BASIC_DAY = "https://my.meteoblue.com/packages/basic-day"
METEOBLUE_API_KEY = os.getenv("METEOBLUE_API_KEY", "").strip() or "EinsFhSxjCfcHkOL"
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"
ODDS_API_IO_KEY = os.getenv("ODDS_API_IO_KEY", "").strip() or "8c3630af6548549eb8adeedf672ff9b80eea2e20b362ef4f4e1782e2c115e949"
ODDS_API_IO_BOOKMAKERS = os.getenv("ODDS_API_IO_BOOKMAKERS", "").strip() or "Bet365,DraftKings"

CLIMA_CITIES = [
    {"name": "London", "slug": "london", "lat": 51.5072, "lon": -0.1276},
    {"name": "Buenos Aires", "slug": "buenos-aires", "lat": -34.6037, "lon": -58.3816},
    {"name": "NYC", "slug": "nyc", "lat": 40.7128, "lon": -74.0060},
    {"name": "Chicago", "slug": "chicago", "lat": 41.8781, "lon": -87.6298},
    {"name": "Paris", "slug": "paris", "lat": 48.8566, "lon": 2.3522},
    {"name": "Toronto", "slug": "toronto", "lat": 43.6532, "lon": -79.3832},
    {"name": "Miami", "slug": "miami", "lat": 25.7617, "lon": -80.1918},
    {"name": "Atlanta", "slug": "atlanta", "lat": 33.7490, "lon": -84.3880},
    {"name": "Seattle", "slug": "seattle", "lat": 47.6062, "lon": -122.3321},
    {"name": "Sao Paulo", "slug": "sao-paulo", "lat": -23.5558, "lon": -46.6396},
    {"name": "Seoul", "slug": "seoul", "lat": 37.5665, "lon": 126.9780},
    {"name": "Wellington", "slug": "wellington", "lat": -41.2865, "lon": 174.7762},
    {"name": "Ankara", "slug": "ankara", "lat": 39.9334, "lon": 32.8597},
    {"name": "Dallas", "slug": "dallas"},
    {"name": "Shanghai", "slug": "shanghai"},
    {"name": "Hong Kong", "slug": "hong-kong"},
    {"name": "Munich", "slug": "munich"},
    {"name": "Madrid", "slug": "madrid"},
    {"name": "Milan", "slug": "milan"},
    {"name": "Chengdu", "slug": "chengdu"},
    {"name": "Chongqing", "slug": "chongqing"},
    {"name": "Tokyo", "slug": "tokyo"},
    {"name": "Singapore", "slug": "singapore"},
    {"name": "Lucknow", "slug": "lucknow"},
    {"name": "Wuhan", "slug": "wuhan"},
    {"name": "Warsaw", "slug": "warsaw"},
    {"name": "Shenzhen", "slug": "shenzhen"},
    {"name": "Beijing", "slug": "beijing"},
    {"name": "Taipei", "slug": "taipei"},
    {"name": "Denver", "slug": "denver"},
    {"name": "Tel Aviv", "slug": "tel-aviv"},
    {"name": "Los Angeles", "slug": "los-angeles"},
    {"name": "San Francisco", "slug": "san-francisco"},
    {"name": "Houston", "slug": "houston"},
    {"name": "Austin", "slug": "austin"},
]
PAYMENT_WALLET = "0x17Ddf5d22fCF360E8D0dAED4e83717aeb1d47836"

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

app = FastAPI(title="Polymarket Bot Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Um processo do bot por usuário (user_id -> Popen)
_bot_processes: dict[str, subprocess.Popen] = {}
_bot_log_handles: dict[str, list] = {}

# Um processo de auto-claim por usuário (user_id -> Popen)
_autoclaim_processes: dict[str, subprocess.Popen] = {}
_autoclaim_log_handles: dict[str, list] = {}

# Caches simples em memÃ³ria para chamadas externas
_gamma_cache: dict[str, tuple[float, Any]] = {}
_odds_cache: dict[str, tuple[float, Any]] = {}
_ev_esportes_cache: dict[str, tuple[float, Any]] = {}
_ev_clima_cache: dict[str, tuple[float, Any]] = {}
_geo_cache: dict[str, tuple[float, Any]] = {}


def _writable_log_dir() -> Path:
    """Retorna um diretório gravável para logs (evita erro em disco read-only, ex.: Vercel)."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        test = DATA_DIR / ".write_check"
        test.write_text("")
        test.unlink(missing_ok=True)
        return DATA_DIR
    except OSError:
        base = Path(os.environ.get("TMPDIR") or os.environ.get("TEMP") or "/tmp")
        log_dir = base / "polymarket_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir


def _is_serverless() -> bool:
    """True se estiver em ambiente serverless (ex.: Vercel), onde bot/autoclaim não podem rodar."""
    return os.environ.get("VERCEL") == "1" or "/var/task" in str(PROJECT_ROOT)


def _cleanup_user_bot(user_id: str) -> None:
    """Remove processo morto do usuário e fecha handles de log."""
    proc = _bot_processes.get(user_id)
    if proc is not None and proc.poll() is not None:
        for f in _bot_log_handles.get(user_id, []):
            try:
                f.close()
            except Exception:
                pass
        _bot_log_handles.pop(user_id, None)
        _bot_processes.pop(user_id, None)


def _cleanup_user_autoclaim(user_id: str) -> None:
    """Remove processo morto de autoclaim do usuário e fecha handles de log."""
    proc = _autoclaim_processes.get(user_id)
    if proc is not None and proc.poll() is not None:
        for f in _autoclaim_log_handles.get(user_id, []):
            try:
                f.close()
            except Exception:
                pass
        _autoclaim_log_handles.pop(user_id, None)
        _autoclaim_processes.pop(user_id, None)


def _supabase_user_from_token(token: str) -> Optional[dict]:
    try:
        r = requests.get(
            f"{SUPABASE_URL}/auth/v1/user",
            headers={"Authorization": f"Bearer {token}", "apikey": SUPABASE_ANON_KEY},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data if data.get("id") else None
    except Exception:
        return None


def _get_current_user_token(authorization: Optional[str] = None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token ausente ou inválido")
    return authorization[7:].strip()


async def get_current_user(authorization: Optional[str] = Header(None, alias="Authorization")):
    token = _get_current_user_token(authorization)
    user = _supabase_user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Sessão inválida ou expirada. Faça login novamente.")
    return {"id": user["id"], "email": user.get("email", ""), "_token": token}


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Dependência: exige que o usuário seja o admin (malagueta.canal@gmail.com)."""
    if (user.get("email") or "").strip().lower() != ADMIN_EMAIL.strip().lower():
        raise HTTPException(status_code=403, detail="Acesso restrito ao administrador.")
    return user


def _supabase_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "apikey": SUPABASE_ANON_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _config_from_supabase(user_id: str, token: str) -> dict:
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_config",
            params={"user_id": f"eq.{user_id}", "select": "*"},
            headers=_supabase_headers(token),
            timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return {}
        return dict(r.json()[0])
    except Exception:
        return {}


def _config_from_supabase_by_email_admin(email: str) -> dict:
    try:
        if not email:
            return {}
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/user_config",
            params={"email": f"eq.{email}", "select": "*"},
            headers=_admin_headers(),
            timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return {}
        return dict(r.json()[0])
    except Exception:
        return {}


def _relink_user_id_admin(email: str, new_user_id: str) -> None:
    if not email or not new_user_id:
        return
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/user_config",
        params={"email": f"eq.{email}"},
        headers=_admin_headers(),
        json={"user_id": new_user_id},
        timeout=10,
    )
    r.raise_for_status()


def _config_to_supabase(user_id: str, token: str, data: dict, email: Optional[str] = None) -> None:
    try:
        payload = {k: v for k, v in data.items() if v is not None}
        payload["user_id"] = user_id
        if email is not None:
            payload["email"] = email
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/user_config",
            params={"on_conflict": "user_id"},
            headers={**_supabase_headers(token), "Prefer": "resolution=merge-duplicates"},
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=500, detail="Erro ao salvar config. Tente novamente.")


def _telegram_send(token: str, chat_id: str, text: str) -> None:
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Telegram token/chat_id ausentes.")
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=8,
        )
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao enviar Telegram: {e!s}")


def _safe_user_id(user_id: str) -> str:
    """Sanitiza user_id para uso em nomes de arquivo."""
    s = re.sub(r"[^a-zA-Z0-9\-]", "", str(user_id).replace(" ", "-"))[:64]
    return s or "default"


def _parse_iso_date(s: Any) -> Optional[datetime]:
    """Converte string ISO do Supabase para datetime com timezone."""
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _ensure_trial_row(user_id: str, token: str, email: str) -> None:
    """Cria a linha em user_config com trial de 2 dias se ainda não existir."""
    trial_end = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_config",
            params={"on_conflict": "user_id"},
            headers={**_supabase_headers(token), "Prefer": "resolution=ignore-duplicates"},
            json={"user_id": user_id, "email": email or "", "trial_ends_at": trial_end},
            timeout=10,
        )
    except Exception:
        pass


def _user_can_use_bot(row: dict) -> tuple[bool, str]:
    """Retorna (pode_usar, motivo). Motivo: 'trial' | 'subscription' | 'expired'."""
    now = datetime.now(timezone.utc)
    trial_end = _parse_iso_date(row.get("trial_ends_at"))
    sub_end = _parse_iso_date(row.get("subscription_ends_at"))
    if trial_end and now <= trial_end:
        return (True, "trial")
    if sub_end and now <= sub_end:
        return (True, "subscription")
    return (False, "expired")


def _admin_headers() -> dict:
    """Headers para chamadas Supabase com service role (só para admin)."""
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise HTTPException(status_code=503, detail="SUPABASE_SERVICE_ROLE_KEY não configurada.")
    return {
        "apikey": SUPABASE_ANON_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _admin_get_all_user_configs() -> list[dict]:
    """Lista todas as linhas de user_config (usa service role)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_config",
        params={"select": "user_id,email,trial_ends_at,subscription_ends_at,created_at,updated_at", "order": "updated_at.desc"},
        headers=_admin_headers(),
        timeout=10,
    )
    r.raise_for_status()
    return r.json() if r.json() else []


def _admin_grant_days(user_id: str, add_days: int = 30) -> None:
    """Estende subscription_ends_at do usuário (usa service role)."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/rpc/grant_subscription_days",
        headers=_admin_headers(),
        json={"p_user_id": user_id, "p_days": int(add_days)},
        timeout=10,
    )
    if r.status_code == 404:
        raise HTTPException(status_code=404, detail="Usuário não encontrado em user_config.")
    r.raise_for_status()


# --- Modelos ---

class DeriveCredsRequest(BaseModel):
    private_key: str = Field(..., description="Chave privada (com ou sem 0x)")
    funder_address: str = Field("", description="Funder address (proxy/carteira)")
    signature_type: int = Field(1, description="0=EOA, 1=Magic, 2=Proxy")


class DeriveCredsResponse(BaseModel):
    api_key: str
    api_secret: str
    api_passphrase: str


class ConfigUpdate(BaseModel):
    private_key: Optional[str] = None
    funder_address: Optional[str] = None
    signature_type: Optional[int] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    api_passphrase: Optional[str] = None
    kalshi_api_key: Optional[str] = None  # Kalshi API Key ID
    kalshi_api_secret: Optional[str] = None  # Kalshi private key PEM
    kalshi_api_passphrase: Optional[str] = None  # opcional (futuro)
    telegram_bot_token: Optional[str] = None
    telegram_chat_id: Optional[str] = None
    starting_bankroll: Optional[float] = None
    min_bet: Optional[float] = None
    bot_mode: Optional[Literal["safe", "spike_ai", "moon", "multi_confirm", "aggressive", "degen", "arbitragem", "arb_kalshi", "arb_poly", "only_hedge_plus", "odd_master", "90_95"]] = None
    aggressive_bet_pct: Optional[float] = None
    max_token_price: Optional[float] = None
    arb_min_profit_pct: Optional[float] = None
    safe_bet: Optional[float] = None
    only_hedge_bet: Optional[float] = None
    odd_master_bet: Optional[float] = None
    bet_90_95: Optional[float] = None
    arbitragem_pct: Optional[float] = None
    kalshi_align_ptb: Optional[bool] = None


class ConfigResponse(BaseModel):
    funder_address: str
    signature_type: int = 0
    starting_bankroll: float
    min_bet: float
    bot_mode: str
    aggressive_bet_pct: float
    max_token_price: float
    arb_min_profit_pct: float
    safe_bet: Optional[float] = None
    only_hedge_bet: Optional[float] = None
    odd_master_bet: Optional[float] = None
    bet_90_95: Optional[float] = None
    arbitragem_pct: Optional[float] = None
    kalshi_align_ptb: bool = False
    has_private_key: bool
    has_api_creds: bool
    has_kalshi_api_creds: bool
    has_telegram: bool
    access_ok: bool = True
    access_reason: Optional[str] = None
    trial_ends_at: Optional[str] = None
    subscription_ends_at: Optional[str] = None
    payment_wallet: Optional[str] = None


class BotStartRequest(BaseModel):
    mode: Literal["safe", "spike_ai", "moon", "multi_confirm", "aggressive", "dry_run", "arbitragem", "arb_kalshi", "arb_poly", "only_hedge_plus", "odd_master", "90_95"] = Field(..., description="Modo de trading")
    dry_run: bool = Field(False, description="Se True, simula sem ordens reais")
    markets: List[Literal["btc", "eth", "btc15m", "eth15m"]] = Field(default=["btc"], description="Mercados: btc, eth, btc15m, eth15m (lista)")
    safe_bet: Optional[float] = None
    only_hedge_bet: Optional[float] = None
    odd_master_bet: Optional[float] = None
    bet_90_95: Optional[float] = None
    aggressive_bet_pct: Optional[float] = None
    arbitragem_pct: Optional[float] = None
    stop_win_enabled: bool = Field(False, description="Ativar take profit (parar ao atingir % de lucro)")
    stop_win_pct: Optional[float] = None
    stop_loss_enabled: bool = Field(False, description="Ativar stop loss (parar ao atingir % de perda)")
    stop_loss_pct: Optional[float] = None
    signals_only: bool = Field(False, description="Se True, não executa ordens; apenas sinais no Telegram")


class BotStatusResponse(BaseModel):
    running: bool
    pid: Optional[int] = None
    mode: Optional[str] = None
    dry_run: Optional[bool] = None


class AutoclaimStatusResponse(BaseModel):
    running: bool
    pid: Optional[int] = None


class AdminGrantAccessRequest(BaseModel):
    user_id: Optional[str] = None
    email: Optional[str] = None


def _read_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    with open(ENV_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _opt_float(env: dict[str, str], key: str) -> Optional[float]:
    v = env.get(key)
    if not v or not str(v).strip():
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _write_env(env: dict[str, str]) -> None:
    """Não escreve .env; a config é passada por variáveis de ambiente ao iniciar o bot."""
    pass


def _derive_creds(private_key: str, funder_address: str = "", signature_type: int = 1) -> tuple[str, str, str]:
    from py_clob_client.client import ClobClient

    key = private_key.strip()
    if not key or key == "0x...":
        raise ValueError("Chave privada inválida")

    # EOA (0): não passar funder — credenciais devem ser do endereço da chave.
    funder_arg = (funder_address.strip() or None) if signature_type in (1, 2) else None

    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        signature_type=signature_type,
        funder=funder_arg,
    )
    creds = client.create_or_derive_api_creds()
    if not creds:
        raise ValueError("Falha ao derivar credenciais")
    return creds.api_key, creds.api_secret, creds.api_passphrase


@app.post("/api/derive-creds", response_model=DeriveCredsResponse)
def derive_creds(req: DeriveCredsRequest):
    """Deriva API key/secret/passphrase a partir da chave privada."""
    if req.signature_type not in (0, 1, 2):
        raise HTTPException(status_code=400, detail="signature_type deve ser 0, 1 ou 2")
    try:
        api_key, api_secret, api_passphrase = _derive_creds(
            req.private_key,
            req.funder_address or "",
            req.signature_type,
        )
        return DeriveCredsResponse(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=400, detail="Falha ao derivar credenciais. Verifique a chave e o funder address.")


@app.get("/api/config", response_model=ConfigResponse)
def get_config(user: dict = Depends(get_current_user)):
    """Retorna config do usuário no Supabase (sem expor chave privada nem secret)."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        email = (user.get("email") or "").strip()
        if email:
            admin_row = _config_from_supabase_by_email_admin(email)
            if admin_row and admin_row.get("user_id") != user["id"]:
                try:
                    _relink_user_id_admin(email, user["id"])
                    row = _config_from_supabase(user["id"], user["_token"])
                except Exception:
                    pass
        _ensure_trial_row(user["id"], user["_token"], user.get("email", ""))
        row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        return ConfigResponse(
            funder_address="",
            signature_type=0,
            starting_bankroll=10.0,
            min_bet=5.0,
            bot_mode="safe",
            aggressive_bet_pct=25.0,
            max_token_price=0.9,
            arb_min_profit_pct=0.04,
            safe_bet=None,
            only_hedge_bet=None,
            odd_master_bet=None,
            bet_90_95=None,
            arbitragem_pct=None,
            kalshi_align_ptb=False,
            has_private_key=False,
            has_api_creds=False,
            has_kalshi_api_creds=False,
            has_telegram=False,
            access_ok=True,
            access_reason="trial",
            trial_ends_at=None,
            subscription_ends_at=None,
            payment_wallet=None,
        )
    can_use, reason = _user_can_use_bot(row)
    trial_ends_at = row.get("trial_ends_at")
    subscription_ends_at = row.get("subscription_ends_at")
    return ConfigResponse(
        funder_address=row.get("funder_address") or "",
        signature_type=int(row.get("signature_type", 0)),
        starting_bankroll=float(row.get("starting_bankroll", 10)),
        min_bet=float(row.get("min_bet", 5)),
        bot_mode=row.get("bot_mode") or "safe",
        aggressive_bet_pct=float(row.get("aggressive_bet_pct", 25)),
        max_token_price=float(row.get("max_token_price", 0.9)),
        arb_min_profit_pct=float(row.get("arb_min_profit_pct", 0.04)),
        safe_bet=row.get("safe_bet") and float(row["safe_bet"]) or None,
        only_hedge_bet=row.get("only_hedge_bet") and float(row["only_hedge_bet"]) or None,
        odd_master_bet=row.get("odd_master_bet") and float(row["odd_master_bet"]) or None,
        bet_90_95=row.get("bet_90_95") and float(row["bet_90_95"]) or None,
        arbitragem_pct=row.get("arbitragem_pct") and float(row["arbitragem_pct"]) or None,
        kalshi_align_ptb=bool(row.get("kalshi_align_ptb")),
        has_private_key=bool(row.get("private_key") and str(row.get("private_key", "")).strip() and row.get("private_key") != "0x..."),
        has_api_creds=bool(row.get("api_key") and row.get("api_secret") and row.get("api_passphrase")),
        has_kalshi_api_creds=bool(row.get("kalshi_api_key") and row.get("kalshi_api_secret")),
        has_telegram=bool(row.get("telegram_bot_token") and row.get("telegram_chat_id")),
        access_ok=can_use,
        access_reason=reason,
        trial_ends_at=str(trial_ends_at) if trial_ends_at else None,
        subscription_ends_at=str(subscription_ends_at) if subscription_ends_at else None,
        payment_wallet=PAYMENT_WALLET if not can_use else None,
    )


@app.post("/api/config")
def update_config(upd: ConfigUpdate, user: dict = Depends(get_current_user)):
    """Atualiza config do usuário no Supabase."""
    data = {}
    if upd.private_key is not None:
        data["private_key"] = upd.private_key
    if upd.funder_address is not None:
        data["funder_address"] = upd.funder_address
    if upd.signature_type is not None:
        data["signature_type"] = upd.signature_type
    if upd.api_key is not None:
        data["api_key"] = upd.api_key
    if upd.api_secret is not None:
        data["api_secret"] = upd.api_secret
    if upd.api_passphrase is not None:
        data["api_passphrase"] = upd.api_passphrase
    if upd.kalshi_api_key is not None:
        data["kalshi_api_key"] = upd.kalshi_api_key
    if upd.kalshi_api_secret is not None:
        data["kalshi_api_secret"] = upd.kalshi_api_secret
    if upd.kalshi_api_passphrase is not None:
        data["kalshi_api_passphrase"] = upd.kalshi_api_passphrase
    if upd.telegram_bot_token is not None:
        data["telegram_bot_token"] = upd.telegram_bot_token
    if upd.telegram_chat_id is not None:
        data["telegram_chat_id"] = upd.telegram_chat_id
    if upd.starting_bankroll is not None:
        data["starting_bankroll"] = upd.starting_bankroll
    if upd.min_bet is not None:
        data["min_bet"] = upd.min_bet
    if upd.bot_mode is not None:
        data["bot_mode"] = upd.bot_mode
    if upd.aggressive_bet_pct is not None:
        data["aggressive_bet_pct"] = int(upd.aggressive_bet_pct)
    if upd.max_token_price is not None:
        data["max_token_price"] = upd.max_token_price
    if upd.arb_min_profit_pct is not None:
        data["arb_min_profit_pct"] = upd.arb_min_profit_pct
    if upd.safe_bet is not None:
        data["safe_bet"] = upd.safe_bet
    if upd.only_hedge_bet is not None:
        data["only_hedge_bet"] = upd.only_hedge_bet
    if upd.odd_master_bet is not None:
        data["odd_master_bet"] = upd.odd_master_bet
    if upd.bet_90_95 is not None:
        data["bet_90_95"] = upd.bet_90_95
    if upd.arbitragem_pct is not None:
        data["arbitragem_pct"] = int(upd.arbitragem_pct)
    if upd.kalshi_align_ptb is not None:
        data["kalshi_align_ptb"] = upd.kalshi_align_ptb
    _config_to_supabase(user["id"], user["_token"], data, user.get("email"))
    return {"ok": True}


@app.post("/api/telegram/test")
def telegram_test(user: dict = Depends(get_current_user)):
    """Envia uma mensagem de teste no Telegram usando as credenciais do usuário."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        raise HTTPException(status_code=400, detail="Config não encontrada.")
    token = (row.get("telegram_bot_token") or "").strip()
    chat_id = (row.get("telegram_chat_id") or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Telegram Bot Token não configurado.")
    if not chat_id:
        raise HTTPException(status_code=400, detail="Telegram Chat ID não configurado.")
    _telegram_send(token, chat_id, "Teste Telegram: bot ativo e pronto para enviar entradas.")
    return {"ok": True}


@app.get("/api/trading-address")
@app.post("/api/trading-address")
def get_trading_address(user: dict = Depends(get_current_user)):
    """Retorna o endereço que o bot usa para ordens (para comparar com o Portfolio na Polymarket)."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row or not row.get("private_key") or str(row.get("private_key", "")).strip() in ("", "0x..."):
        raise HTTPException(status_code=400, detail="Salve a chave privada na Config primeiro.")
    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        raise HTTPException(status_code=503, detail="py_clob_client não instalado.")
    key = (row.get("private_key") or "").strip()
    sig_type = int(row.get("signature_type", 0))
    funder = (row.get("funder_address") or "").strip()
    funder_arg = (funder or None) if sig_type in (1, 2) else None
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        signature_type=sig_type,
        funder=funder_arg,
    )
    addr = client.get_address()
    if not addr:
        raise HTTPException(status_code=500, detail="Não foi possível obter o endereço.")
    return {"address": addr}


@app.get("/api/check-balance")
def api_check_balance(user: dict = Depends(get_current_user)):
    """Diagnóstico: verifica saldo e allowance na API (para debugar 'not enough balance')."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row or not row.get("private_key") or not row.get("api_key"):
        raise HTTPException(status_code=400, detail="Salve chave privada e API na Config primeiro.")
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    except ImportError:
        raise HTTPException(status_code=503, detail="py_clob_client não instalado.")
    key = (row.get("private_key") or "").strip()
    funder = (row.get("funder_address") or "").strip()
    st = row.get("signature_type")
    sig_type = int(st) if st is not None else 0
    funder_arg = (funder or None) if sig_type in (1, 2) else None
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        creds=ApiCreds(
            api_key=row.get("api_key", ""),
            api_secret=row.get("api_secret", ""),
            api_passphrase=row.get("api_passphrase", ""),
        ),
        signature_type=sig_type,
        funder=funder_arg,
    )
    addr = client.get_address()
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    update_ok = True
    update_err = None
    try:
        client.update_balance_allowance(params=params)
    except Exception as e:
        update_ok = False
        update_err = str(e)
    bal = None
    allowance = None
    try:
        resp = client.get_balance_allowance(params=params)
        if resp:
            bal = resp.get("balance")
            allowance = resp.get("allowance")
    except Exception:
        pass
    # USDC usa 6 decimais: valor bruto / 1e6 = USD
    bal_float = float(bal) if bal is not None else None
    if bal_float is not None and bal_float > 1000:
        bal_float = bal_float / 1e6
    return {
        "address": addr,
        "signature_type": sig_type,
        "update_ok": update_ok,
        "update_error": update_err,
        "balance": bal_float,
        "allowance": str(allowance) if allowance is not None else None,
    }


@app.post("/api/force-sync-balance")
def api_force_sync_balance(user: dict = Depends(get_current_user)):
    """Força múltiplos update_balance_allowance (para proxy/safe com 'not enough allowance')."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row or not row.get("private_key") or not row.get("api_key"):
        raise HTTPException(status_code=400, detail="Salve chave privada e API na Config primeiro.")
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    except ImportError:
        raise HTTPException(status_code=503, detail="py_clob_client não instalado.")
    key = (row.get("private_key") or "").strip()
    funder = (row.get("funder_address") or "").strip()
    st = row.get("signature_type")
    sig_type = int(st) if st is not None else 0
    funder_arg = (funder or None) if sig_type in (1, 2) else None
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        creds=ApiCreds(
            api_key=row.get("api_key", ""),
            api_secret=row.get("api_secret", ""),
            api_passphrase=row.get("api_passphrase", ""),
        ),
        signature_type=sig_type,
        funder=funder_arg,
    )
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
    for i in range(5):
        try:
            client.update_balance_allowance(params=params)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Sync {i+1}/5 falhou: {e!s}")
        time.sleep(1.5)
    return {"ok": True, "message": "Sync forçado 5x concluído. Tente o bot novamente."}


@app.post("/api/set-allowances")
def api_set_allowances(user: dict = Depends(get_current_user)):
    """Configura allowances on-chain para MetaMask (tipo 0). Necessário uma vez antes de operar via API."""
    row = _config_from_supabase(user["id"], user["_token"])
    if not row or not row.get("private_key") or str(row.get("private_key", "")).strip() in ("", "0x..."):
        raise HTTPException(status_code=400, detail="Salve a chave privada na Config primeiro.")
    sig_type = int(row.get("signature_type", 0))
    if sig_type != 0:
        raise HTTPException(
            status_code=400,
            detail="Allowances são necessários só para MetaMask (tipo 0). Magic/Safe configuram automaticamente.",
        )
    try:
        from set_allowances import run_set_allowances
    except ImportError:
        raise HTTPException(status_code=503, detail="Módulo set_allowances não encontrado.")
    key = (row.get("private_key") or "").strip()
    api_key = (row.get("api_key") or "").strip()
    api_secret = (row.get("api_secret") or "").strip()
    api_passphrase = (row.get("api_passphrase") or "").strip()
    ok, msg, details = run_set_allowances(
        key,
        api_key=api_key or None,
        api_secret=api_secret or None,
        api_passphrase=api_passphrase or None,
    )
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    if not (api_key and api_secret and api_passphrase):
        details.append("Aviso: credenciais API não salvas. Salve-as na Config e clique novamente para atualizar o saldo na API.")
    return {"ok": True, "message": msg, "details": details}


@app.post("/api/bot/start")
def bot_start(req: BotStartRequest, user: dict = Depends(get_current_user)):
    """Inicia o bot com o modo e parâmetros informados; usa config do Supabase do usuário. Um bot por usuário."""
    global _bot_processes
    if _is_serverless():
        raise HTTPException(
            status_code=503,
            detail="O bot não está disponível na Vercel (ambiente serverless). Para rodar o bot, use um servidor com disco gravável, por exemplo uma VPS — veja o guia DEPLOY_VPS.md.",
        )
    user_id = user["id"]
    _cleanup_user_bot(user_id)
    if user_id in _bot_processes and _bot_processes[user_id].poll() is None:
        raise HTTPException(status_code=400, detail="Seu bot já está rodando. Pare antes de iniciar de novo.")

    mode = "safe" if req.mode == "dry_run" else req.mode
    dry_run = req.dry_run or (req.mode == "dry_run")

    row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        raise HTTPException(
            status_code=400,
            detail="Salve suas credenciais Polymarket (chave privada e API) na aba Config antes de iniciar o bot.",
        )
    if not row.get("private_key") or not row.get("api_key") or not row.get("api_secret") or not row.get("api_passphrase"):
        raise HTTPException(
            status_code=400,
            detail="Salve suas credenciais Polymarket (chave privada e API) na aba Config antes de iniciar o bot.",
        )
    can_use, reason = _user_can_use_bot(row)
    if not can_use:
        raise HTTPException(
            status_code=402,
            detail="Seu acesso ao bot encerrou. Para continuar: envie 100 USDC para a carteira informada na aba Iniciar bot e confirme o pagamento (hash da transação). O acesso será liberado em até 24h após confirmação.",
        )
    markets_list = req.markets if isinstance(req.markets, list) else [s.strip() for s in str(req.markets).split(",") if s.strip()]
    markets_list = [m for m in markets_list if m in ("btc", "eth", "btc15m", "eth15m")]
    if mode in ("arb_kalshi", "arb_poly"):
        markets_list = [m for m in markets_list if m in ("btc15m", "eth15m")]
    elif mode == "multi_confirm":
        markets_list = [m for m in markets_list if m in ("btc", "eth", "btc15m", "eth15m")]
    else:
        markets_list = [m for m in markets_list if m != "eth15m"]
    # Não adicionar nenhum mercado: só os selecionados pelo usuário (ex.: só btc15m = zero operação em btc 5min)
    if not markets_list:
        raise HTTPException(
            status_code=400,
            detail="Selecione pelo menos um mercado (BTC 5min, ETH 5min, BTC 15min ou ETH 15min).",
        )

    # Em operação real, validar parâmetros obrigatórios por modo
    if not dry_run:
        min_bet = float(row.get("min_bet", 5))
        if mode in ("safe", "spike_ai", "moon", "multi_confirm"):
            safe_bet = req.safe_bet if req.safe_bet is not None else (row.get("safe_bet") and float(row["safe_bet"]))
            if safe_bet is None or safe_bet < min_bet:
                raise HTTPException(
                    status_code=400,
                    detail=f"Modo Safe/SPIKE AI/MOON exige valor de aposta (Config ou envio). Mínimo: ${min_bet:.2f}.",
                )
        if mode == "only_hedge_plus":
            oh_bet = req.only_hedge_bet if req.only_hedge_bet is not None else (row.get("only_hedge_bet") and float(row["only_hedge_bet"]))
            if oh_bet is None or oh_bet < min_bet:
                raise HTTPException(
                    status_code=400,
                    detail=f"Only Hedge+ exige valor de aposta (Config ou envio). Mínimo: ${min_bet:.2f}.",
                )
        if mode == "odd_master":
            om_bet = req.odd_master_bet if req.odd_master_bet is not None else (row.get("odd_master_bet") and float(row["odd_master_bet"]))
            if om_bet is None or om_bet < min_bet:
                raise HTTPException(
                    status_code=400,
                    detail=f"ODD MASTER exige valor de aposta (Config ou envio). Mínimo: ${min_bet:.2f}.",
                )
        if mode == "90_95":
            bet_90 = req.bet_90_95 if req.bet_90_95 is not None else (row.get("bet_90_95") and float(row["bet_90_95"]))
            if bet_90 is None or bet_90 < min_bet:
                raise HTTPException(
                    status_code=400,
                    detail=f"Modo 90-95 exige valor de aposta (Config ou envio). Mínimo: ${min_bet:.2f}.",
                )
        if mode in ("arbitragem", "arb_kalshi", "arb_poly"):
            arb_pct = req.arbitragem_pct if req.arbitragem_pct is not None else (row.get("arbitragem_pct") and float(row["arbitragem_pct"]))
            if arb_pct is None or arb_pct < 1 or arb_pct > 100:
                raise HTTPException(
                    status_code=400,
                    detail="Modo Arbitragem exige % da banca (1–100) na Config ou no envio.",
                )
        if mode == "arb_kalshi":
            if not row.get("kalshi_api_key") or not row.get("kalshi_api_secret"):
                raise HTTPException(
                    status_code=400,
                    detail="Salve suas credenciais Kalshi (API Key ID e Private Key) na aba Config antes de iniciar o modo Arb Kalshi.",
                )

    env = os.environ.copy()
    env["POLY_PRIVATE_KEY"] = row.get("private_key", "")
    env["POLY_FUNDER_ADDRESS"] = row.get("funder_address", "")
    env["POLY_API_KEY"] = row.get("api_key", "")
    env["POLY_API_SECRET"] = row.get("api_secret", "")
    env["POLY_API_PASSPHRASE"] = row.get("api_passphrase", "")
    st = row.get("signature_type")
    env["POLY_SIGNATURE_TYPE"] = str(int(st) if st is not None else 0)
    env["STARTING_BANKROLL"] = str(row.get("starting_bankroll", 10))
    env["MIN_BET"] = str(row.get("min_bet", 5))
    env["BOT_MODE"] = mode
    pct_aggressive = req.aggressive_bet_pct if req.aggressive_bet_pct is not None else (row.get("aggressive_bet_pct") if row.get("aggressive_bet_pct") is not None else 25)
    if isinstance(pct_aggressive, float):
        pct_aggressive = int(round(pct_aggressive))
    env["AGGRESSIVE_BET_PCT"] = str(max(1, min(100, pct_aggressive)))
    env["MAX_TOKEN_PRICE"] = str(row.get("max_token_price", 0.9))
    env["ARB_MIN_PROFIT_PCT"] = str(row.get("arb_min_profit_pct", 0.04))
    env["BOT_MARKETS"] = ",".join(markets_list)
    if mode == "arb_kalshi":
        env["KALSHI_API_KEY_ID"] = row.get("kalshi_api_key", "")
        env["KALSHI_PRIVATE_KEY_PEM"] = row.get("kalshi_api_secret", "")
        env["KALSHI_ALIGN_PTB"] = "1" if row.get("kalshi_align_ptb") else "0"
    if row.get("telegram_bot_token") and row.get("telegram_chat_id"):
        env["TELEGRAM_BOT_TOKEN"] = row.get("telegram_bot_token", "")
        env["TELEGRAM_CHAT_ID"] = row.get("telegram_chat_id", "")
    safe_id = _safe_user_id(user["id"])
    env["BOT_USER_ID"] = safe_id
    if mode == "90_95":
        bet_90 = req.bet_90_95 if req.bet_90_95 is not None else (row.get("bet_90_95") and float(row["bet_90_95"]))
        if bet_90 is not None:
            env["BET_90_95"] = str(bet_90)

    # Stop Win / Stop Loss: bankroll inicial da config; % enviados pelo usuário
    if getattr(req, "stop_win_enabled", False) or getattr(req, "stop_loss_enabled", False):
        initial = float(row.get("starting_bankroll", 10))
        env["STOP_WIN_LOSS_INITIAL_BANKROLL"] = str(initial)
        env["STOP_WIN_ENABLED"] = "1" if getattr(req, "stop_win_enabled", False) else "0"
        env["STOP_LOSS_ENABLED"] = "1" if getattr(req, "stop_loss_enabled", False) else "0"
        if getattr(req, "stop_win_enabled", False) and req.stop_win_pct is not None:
            env["STOP_WIN_PCT"] = str(max(0.1, min(500, float(req.stop_win_pct))))
        else:
            env["STOP_WIN_PCT"] = "0"
        if getattr(req, "stop_loss_enabled", False) and req.stop_loss_pct is not None:
            env["STOP_LOSS_PCT"] = str(max(0.1, min(100, float(req.stop_loss_pct))))
        else:
            env["STOP_LOSS_PCT"] = "0"

    if getattr(req, "signals_only", False):
        env["SIGNALS_ONLY"] = "1"

    # Atualizar Supabase com o modo (e parâmetros) usados ao iniciar — assim bot_mode fica sincronizado
    try:
        start_config: dict = {"bot_mode": mode}
        if mode in ("safe", "spike_ai", "moon", "multi_confirm") and (req.safe_bet is not None or (row.get("safe_bet") and float(row["safe_bet"]))):
            start_config["safe_bet"] = req.safe_bet if req.safe_bet is not None else float(row["safe_bet"])
        if mode == "aggressive":
            start_config["aggressive_bet_pct"] = int(round(pct_aggressive))
        if mode == "only_hedge_plus" and (req.only_hedge_bet is not None or (row.get("only_hedge_bet") and float(row["only_hedge_bet"]))):
            start_config["only_hedge_bet"] = req.only_hedge_bet if req.only_hedge_bet is not None else float(row["only_hedge_bet"])
        if mode == "odd_master" and (req.odd_master_bet is not None or (row.get("odd_master_bet") and float(row["odd_master_bet"]))):
            start_config["odd_master_bet"] = req.odd_master_bet if req.odd_master_bet is not None else float(row["odd_master_bet"])
        if mode == "90_95" and (req.bet_90_95 is not None or (row.get("bet_90_95") and float(row["bet_90_95"]))):
            start_config["bet_90_95"] = req.bet_90_95 if req.bet_90_95 is not None else float(row["bet_90_95"])
        if mode in ("arbitragem", "arb_kalshi", "arb_poly") and (req.arbitragem_pct is not None or (row.get("arbitragem_pct") and float(row["arbitragem_pct"]))):
            start_config["arbitragem_pct"] = int(req.arbitragem_pct if req.arbitragem_pct is not None else float(row["arbitragem_pct"]))
        _config_to_supabase(user_id, user["_token"], start_config, user.get("email"))
    except Exception:
        pass  # não falhar o start se o update da config falhar

    cmd = [sys.executable, str(PROJECT_ROOT / "bot.py"), "--mode", mode, "--markets", ",".join(markets_list)]
    if dry_run:
        cmd.append("--dry-run")
    if mode in ("safe", "spike_ai", "moon", "multi_confirm"):
        safe_bet_val = req.safe_bet if req.safe_bet is not None else (float(row["safe_bet"]) if row.get("safe_bet") else None)
        if safe_bet_val is not None:
            cmd.extend(["--safe-bet", str(round(safe_bet_val, 2))])
    if mode == "only_hedge_plus":
        bet = req.only_hedge_bet if req.only_hedge_bet is not None else row.get("only_hedge_bet")
        if bet is not None:
            cmd.extend(["--only-hedge-bet", str(bet)])
    if mode == "odd_master":
        bet = req.odd_master_bet if req.odd_master_bet is not None else row.get("odd_master_bet")
        if bet is not None:
            cmd.extend(["--odd-master-bet", str(bet)])
    if mode == "90_95":
        bet = req.bet_90_95 if req.bet_90_95 is not None else row.get("bet_90_95")
        if bet is not None:
            cmd.extend(["--bet-90-95", str(bet)])
    if mode in ("arbitragem", "arb_kalshi", "arb_poly"):
        pct = req.arbitragem_pct if req.arbitragem_pct is not None else row.get("arbitragem_pct")
        if pct is not None:
            cmd.extend(["--arbitragem-pct", str(int(pct))])

    log_dir = _writable_log_dir()
    log_path = log_dir / f"resultados_{safe_id}.txt"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        extra = ""
        if mode == "aggressive":
            extra = f" aggressive_pct={pct_aggressive}%"
        elif mode in ("safe", "spike_ai", "moon") and (req.safe_bet is not None or (row.get("safe_bet") and float(row["safe_bet"]))):
            bet = req.safe_bet if req.safe_bet is not None else float(row["safe_bet"])
            extra = f" safe_bet=${bet:.2f}"
        elif mode == "only_hedge_plus" and (req.only_hedge_bet is not None or (row.get("only_hedge_bet") and float(row["only_hedge_bet"]))):
            bet = req.only_hedge_bet if req.only_hedge_bet is not None else float(row["only_hedge_bet"])
            extra = f" only_hedge_bet=${bet:.2f}"
        elif mode == "odd_master" and (req.odd_master_bet is not None or (row.get("odd_master_bet") and float(row["odd_master_bet"]))):
            bet = req.odd_master_bet if req.odd_master_bet is not None else float(row["odd_master_bet"])
            extra = f" odd_master_bet=${bet:.2f}"
        elif mode == "90_95" and (req.bet_90_95 is not None or (row.get("bet_90_95") and float(row["bet_90_95"]))):
            bet = req.bet_90_95 if req.bet_90_95 is not None else float(row["bet_90_95"])
            extra = f" bet_90_95=${bet:.2f}"
        elif mode in ("arbitragem", "arb_kalshi", "arb_poly") and (req.arbitragem_pct is not None or (row.get("arbitragem_pct") and float(row["arbitragem_pct"]))):
            pct = req.arbitragem_pct if req.arbitragem_pct is not None else float(row["arbitragem_pct"])
            extra = f" arbitragem_pct={int(round(pct))}%"
        with open(log_path, "w", encoding="utf-8") as log_file:
            log_file.write(f"--- Bot iniciado em {datetime.now(timezone.utc).isoformat()} | modo={mode}{extra} dry_run={dry_run} markets={','.join(markets_list)} ---\n")
        stdout_dest = open(log_path, "a", encoding="utf-8")
        stderr_dest = open(log_path, "a", encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Não foi possível criar o arquivo de log: {e!s}")

    _bot_log_handles[user_id] = [stdout_dest, stderr_dest]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=stdout_dest,
            stderr=stderr_dest,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except Exception as e:
        for f in [stdout_dest, stderr_dest]:
            try:
                f.close()
            except Exception:
                pass
        _bot_log_handles.pop(user_id, None)
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar o bot: {e!s}")

    _bot_processes[user_id] = proc
    return {"ok": True, "pid": proc.pid, "mode": mode, "dry_run": dry_run}


@app.post("/api/bot/stop")
def bot_stop(user: dict = Depends(get_current_user)):
    """Para o bot deste usuário se estiver rodando."""
    global _bot_processes, _bot_log_handles
    user_id = user["id"]
    proc = _bot_processes.get(user_id)
    if proc is None:
        return {"ok": True, "message": "Bot não estava rodando"}
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for f in _bot_log_handles.get(user_id, []):
        try:
            f.close()
        except Exception:
            pass
    _bot_log_handles.pop(user_id, None)
    _bot_processes.pop(user_id, None)
    return {"ok": True}


@app.get("/api/bot/status", response_model=BotStatusResponse)
def bot_status(user: dict = Depends(get_current_user)):
    """Retorna se o bot deste usuário está rodando."""
    user_id = user["id"]
    _cleanup_user_bot(user_id)
    proc = _bot_processes.get(user_id)
    if proc is None or proc.poll() is not None:
        return BotStatusResponse(running=False)
    return BotStatusResponse(
        running=True,
        pid=proc.pid,
    )


@app.get("/api/bot/logs")
def bot_logs(user: dict = Depends(get_current_user), tail: int = 100):
    """Retorna as últimas linhas do log do bot (resultados_<user>.txt)."""
    if tail < 1 or tail > 500:
        tail = 100
    safe_id = _safe_user_id(user["id"])
    log_dir = _writable_log_dir()
    log_path = log_dir / f"resultados_{safe_id}.txt"
    if not log_path.exists():
        return {"lines": [], "message": "Nenhum log ainda. Inicie o bot para gerar saída."}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
        return {"lines": [ln.rstrip("\n\r") for ln in lines]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/user-logs")
def admin_user_logs(user_id: str, tail: int = 150, admin: dict = Depends(require_admin)):
    """Admin: retorna as últimas linhas do log do bot de um usuário específico."""
    if tail < 1 or tail > 500:
        tail = 150
    safe_id = _safe_user_id(user_id)
    log_dir = _writable_log_dir()
    log_path = log_dir / f"resultados_{safe_id}.txt"
    if not log_path.exists():
        return {"lines": [], "message": "Nenhum log encontrado para este usuário."}
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        lines = all_lines[-tail:] if len(all_lines) > tail else all_lines
        return {"lines": [ln.rstrip("\n\r") for ln in lines]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/autoclaim/start")
def autoclaim_start(user: dict = Depends(get_current_user)):
    """Inicia o auto-claim apenas para este usuário (um processo por usuário)."""
    global _autoclaim_processes, _autoclaim_log_handles
    if _is_serverless():
        raise HTTPException(
            status_code=503,
            detail="Auto-claim e o bot não estão disponíveis na Vercel (ambiente serverless). Para rodar o bot e o auto-claim, use um servidor com disco gravável, por exemplo uma VPS — veja o guia DEPLOY_VPS.md.",
        )
    user_id = user["id"]
    if isinstance(user_id, str):
        pass
    else:
        user_id = str(user_id)
    _cleanup_user_autoclaim(user_id)
    if user_id in _autoclaim_processes and _autoclaim_processes[user_id].poll() is None:
        raise HTTPException(status_code=400, detail="Auto-claim já está ativo para você. Desative antes de ativar de novo.")

    safe_id = _safe_user_id(user_id)
    env = os.environ.copy()
    env["BOT_USER_ID"] = safe_id

    row = _config_from_supabase(user["id"], user["_token"])
    key = (row.get("private_key") or "").strip()
    if not key or key == "0x...":
        raise HTTPException(
            status_code=400,
            detail="Salve suas credenciais Polymarket (chave privada e Funder Address) na aba Config antes de ativar o auto-claim.",
        )
    env["POLY_PRIVATE_KEY"] = row.get("private_key", "")
    env["POLY_FUNDER_ADDRESS"] = row.get("funder_address", "")
    env["POLY_SIGNATURE_TYPE"] = str(int(row.get("signature_type", 1)))
    env["CLAIM_INTERVAL_SEC"] = os.getenv("CLAIM_INTERVAL_SEC", "60")

    log_dir = _writable_log_dir()
    log_path = log_dir / f"autoclaim_{safe_id}.txt"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n--- Auto-claim (claim por API) iniciado em {datetime.now(timezone.utc).isoformat()} ---\n")
        stdout_dest = open(log_path, "a", encoding="utf-8")
        stderr_dest = open(log_path, "a", encoding="utf-8")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Não foi possível criar o arquivo de log: {e!s}")

    _autoclaim_log_handles[user_id] = [stdout_dest, stderr_dest]

    try:
        cmd = [sys.executable, str(PROJECT_ROOT / "claim_loop.py")]
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=stdout_dest,
            stderr=stderr_dest,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
    except Exception as e:
        for f in [stdout_dest, stderr_dest]:
            try:
                f.close()
            except Exception:
                pass
        _autoclaim_log_handles.pop(user_id, None)
        raise HTTPException(status_code=500, detail=f"Erro ao iniciar o script de auto-claim: {e!s}")

    _autoclaim_processes[user_id] = proc
    return {"ok": True, "pid": proc.pid}


@app.post("/api/autoclaim/stop")
def autoclaim_stop(user: dict = Depends(get_current_user)):
    """Para o auto-claim deste usuário."""
    global _autoclaim_processes, _autoclaim_log_handles
    user_id = user["id"]
    proc = _autoclaim_processes.get(user_id)
    if proc is None:
        return {"ok": True, "message": "Auto-claim não estava ativo"}
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    for f in _autoclaim_log_handles.get(user_id, []):
        try:
            f.close()
        except Exception:
            pass
    _autoclaim_log_handles.pop(user_id, None)
    _autoclaim_processes.pop(user_id, None)
    return {"ok": True}


@app.get("/api/autoclaim/status", response_model=AutoclaimStatusResponse)
def autoclaim_status(user: dict = Depends(get_current_user)):
    """Retorna se o auto-claim deste usuário está ativo."""
    user_id = user["id"]
    _cleanup_user_autoclaim(user_id)
    proc = _autoclaim_processes.get(user_id)
    if proc is None or proc.poll() is not None:
        return AutoclaimStatusResponse(running=False)
    return AutoclaimStatusResponse(running=True, pid=proc.pid)


@app.post("/api/claim/run")
def claim_run_now(user: dict = Depends(get_current_user)):
    """Executa o claim por API uma vez (posições redeemable) com a config do usuário."""
    row = _config_from_supabase(user["id"], user["_token"])
    key = (row.get("private_key") or "").strip()
    funder = (row.get("funder_address") or "").strip()
    sig_type = int(row.get("signature_type", 1)) if row.get("signature_type") is not None else 1
    if not key or key == "0x...":
        raise HTTPException(
            status_code=400,
            detail="Salve suas credenciais Polymarket (chave privada e Funder Address) na aba Config.",
        )
    try:
        from claim_api import run_claim
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="Módulo claim_api/polymarket-apis não disponível. Instale: pip install polymarket-apis (Python >= 3.12).",
        )
    result = run_claim(private_key=key, funder_address=funder, signature_type=sig_type)
    return result


def _cache_get(cache: dict, key: str, ttl_sec: int) -> Optional[Any]:
    item = cache.get(key)
    if not item:
        return None
    ts, value = item
    if time.time() - ts > ttl_sec:
        cache.pop(key, None)
        return None
    return value


def _cache_set(cache: dict, key: str, value: Any) -> None:
    cache[key] = (time.time(), value)


def _cache_peek(cache: dict, key: str) -> Optional[Any]:
    item = cache.get(key)
    if not item:
        return None
    return item[1]


def _gamma_get_sports() -> list[dict]:
    cached = _cache_get(_gamma_cache, "sports", 300)
    if cached is not None:
        return cached
    try:
        r = requests.get(GAMMA_SPORTS, timeout=10)
        data = r.json() if r.ok else []
    except Exception:
        data = []
    _cache_set(_gamma_cache, "sports", data)
    return data


def _gamma_get_series(series_id: str) -> Optional[dict]:
    cache_key = f"series:{series_id}"
    cached = _cache_get(_gamma_cache, cache_key, 120)
    if cached is not None:
        return cached
    try:
        r = requests.get(f"{GAMMA_SERIES}/{series_id}", timeout=10)
        data = r.json() if r.ok else None
    except Exception:
        data = None
    _cache_set(_gamma_cache, cache_key, data)
    return data


def _odds_get(path: str, params: Optional[dict] = None) -> Optional[dict]:
    if not ODDS_API_IO_KEY:
        return None
    cache_key = f"odds:{path}:{json.dumps(params or {}, sort_keys=True)}"
    cached = _cache_get(_odds_cache, cache_key, 60)
    if cached is not None:
        return cached
    try:
        q = {"apiKey": ODDS_API_IO_KEY}
        if params:
            q.update(params)
        r = requests.get(f"{ODDS_API_IO_BASE}{path}", params=q, timeout=15)
        data = r.json() if r.ok else None
    except Exception:
        data = None
    _cache_set(_odds_cache, cache_key, data)
    return data


def _build_clima_slug(city_slug: str, date_et: datetime) -> str:
    month = date_et.strftime("%B").lower()
    day = str(int(date_et.strftime("%d")))
    year = date_et.strftime("%Y")
    return f"highest-temperature-in-{city_slug}-on-{month}-{day}-{year}"


def _get_event_by_slug_gamma(slug: str) -> Optional[dict]:
    try:
        r = requests.get(GAMMA_EVENTS, params={"slug": slug}, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict) and data.get("slug"):
            return data
        return None
    except Exception:
        return None


def _parse_outcomes(market: dict) -> tuple[list[str], list[float], list[str]]:
    outcomes_raw = market.get("outcomes")
    prices_raw = market.get("outcomePrices") or market.get("outcome_prices")
    token_ids_raw = market.get("clobTokenIds")
    if isinstance(outcomes_raw, str):
        outcomes = json.loads(outcomes_raw)
    else:
        outcomes = outcomes_raw or []
    if isinstance(prices_raw, str):
        prices = [float(p) for p in json.loads(prices_raw)]
    else:
        prices = [float(p) for p in (prices_raw or [])]
    if isinstance(token_ids_raw, str):
        token_ids = json.loads(token_ids_raw)
    else:
        token_ids = token_ids_raw or []
    return outcomes, prices, token_ids


def _detect_unit(texts: list[str], title: str) -> str:
    text = " ".join([t for t in texts if t]) + " " + (title or "")
    if "°F" in text:
        return "fahrenheit"
    if "°C" in text:
        return "celsius"
    return "celsius"


def _parse_range(text: str) -> tuple[Optional[float], Optional[float]]:
    t = text.replace("°", "").replace("º", "").lower()
    nums = []
    for part in t.replace("to", "-").replace("–", "-").split("-"):
        raw = part.strip().split()[0] if part.strip() else ""
        cleaned = re.sub(r"[^0-9.+-]", "", raw)
        try:
            nums.append(float(cleaned))
        except Exception:
            pass
    if "or higher" in t or "+" in t or ">=" in t or "at least" in t:
        return (nums[0] if nums else None, None)
    if "or lower" in t or "<=" in t or "at most" in t:
        return (None, nums[0] if nums else None)
    if len(nums) >= 2:
        return (nums[0], nums[1])
    if len(nums) == 1:
        return (nums[0], nums[0])
    return (None, None)


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.5
    z = (x - mu) / (sigma * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))

def _skew_sigmas(sigma: float, skew: float) -> tuple[float, float]:
    """Retorna (sigma_left, sigma_right) para uma normal assimétrica simples."""
    if sigma <= 0:
        return (1.0, 1.0)
    s = max(-0.9, min(0.9, float(skew)))
    if s >= 0:
        sigma_left = sigma * (1 - 0.3 * s)
        sigma_right = sigma * (1 + 0.7 * s)
    else:
        sigma_left = sigma * (1 + 0.7 * abs(s))
        sigma_right = sigma * (1 - 0.3 * abs(s))
    sigma_left = max(0.5, sigma_left)
    sigma_right = max(0.5, sigma_right)
    return (sigma_left, sigma_right)

def _skew_cdf(x: float, mu: float, sigma: float, skew: float) -> float:
    """CDF de normal assimétrica (split normal) com sigmas diferentes."""
    sigma_left, sigma_right = _skew_sigmas(sigma, skew)
    if sigma_left <= 0 or sigma_right <= 0:
        return 0.5
    w_left = sigma_left / (sigma_left + sigma_right)
    w_right = 1.0 - w_left
    if x < mu:
        return w_left * _norm_cdf(x, mu, sigma_left)
    return w_left + (w_right * _norm_cdf(x, mu, sigma_right))

def _season_for_city_date(city: dict, date_obj: datetime) -> tuple[str, list[int]]:
    lat = city.get("lat")
    north = True if lat is None else lat >= 0
    m = date_obj.month
    if north:
        if m in (12, 1, 2):
            return ("winter", [12, 1, 2])
        if m in (3, 4, 5):
            return ("spring", [3, 4, 5])
        if m in (6, 7, 8):
            return ("summer", [6, 7, 8])
        return ("fall", [9, 10, 11])
    if m in (12, 1, 2):
        return ("summer", [12, 1, 2])
    if m in (3, 4, 5):
        return ("fall", [3, 4, 5])
    if m in (6, 7, 8):
        return ("winter", [6, 7, 8])
    return ("spring", [9, 10, 11])

def _historical_sigma_openmeteo(city: dict, unit: str, target_date: datetime) -> Optional[float]:
    city = _resolve_city_coords(city) or city
    if city.get("lat") is None or city.get("lon") is None:
        return None
    season, months = _season_for_city_date(city, target_date)
    cache_key = f"sigma_hist:{city.get('slug') or city.get('name')}:{season}:{unit}"
    cached = _cache_get(_ev_clima_cache, cache_key, 7 * 24 * 60 * 60)
    if cached is not None:
        return cached
    end_year = target_date.year - 1
    start_year = end_year - 2
    if months == [12, 1, 2]:
        start_date = f"{start_year - 1}-12-01"
        end_date = f"{end_year}-02-{calendar.monthrange(end_year, 2)[1]:02d}"
    else:
        start_month = min(months)
        end_month = max(months)
        start_date = f"{start_year}-{start_month:02d}-01"
        end_date = f"{end_year}-{end_month:02d}-{calendar.monthrange(end_year, end_month)[1]:02d}"
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "start_date": start_date,
        "end_date": end_date,
        "daily": "temperature_2m_max",
        "timezone": "UTC",
    }
    if unit == "f":
        params["temperature_unit"] = "fahrenheit"
    try:
        r = requests.get(OPEN_METEO_ARCHIVE, params=params, timeout=10)
        if not r.ok:
            return None
        payload = r.json() or {}
        daily = payload.get("daily") or {}
        temps = daily.get("temperature_2m_max") or []
        dates = daily.get("time") or []
        if not temps or not dates:
            return None
        values = []
        for t, d in zip(temps, dates):
            try:
                m = int(d.split("-")[1])
            except Exception:
                continue
            if m in months:
                values.append(float(t))
        if len(values) < 10:
            return None
        sigma_hist = statistics.pstdev(values)
        sigma_hist = max(1.0, sigma_hist)
        _cache_set(_ev_clima_cache, cache_key, sigma_hist)
        return sigma_hist
    except Exception:
        return None

def _skew_for_city_date(city: dict, target_date: datetime) -> float:
    season, _ = _season_for_city_date(city, target_date)
    if season == "summer":
        return 0.6
    if season in ("spring", "fall"):
        return 0.3
    return 0.0


def _resolve_city_coords(city: dict) -> Optional[dict]:
    if city.get("lat") is not None and city.get("lon") is not None:
        return city
    key = (city.get("name") or city.get("slug") or "").strip().lower()
    if not key:
        return None
    cached = _cache_get(_geo_cache, key, 30 * 24 * 3600)
    if cached is not None:
        city.update(cached)
        return city
    try:
        r = requests.get(
            OPEN_METEO_GEOCODE,
            params={"name": city.get("name") or city.get("slug"), "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
        if not r.ok:
            return None
        data = r.json() or {}
        results = data.get("results") or []
        if not results:
            return None
        res = results[0]
        coords = {
            "lat": res.get("latitude"),
            "lon": res.get("longitude"),
            "asl": res.get("elevation") or city.get("asl", 0),
        }
        if coords["lat"] is None or coords["lon"] is None:
            return None
        _cache_set(_geo_cache, key, coords)
        city.update(coords)
        return city
    except Exception:
        return None


def _forecast_openmeteo(city: dict, unit: str, target_date: datetime) -> Optional[tuple[float, float]]:
    try:
        city = _resolve_city_coords(city) or city
        if city.get("lat") is None or city.get("lon") is None:
            return None
        date_str = target_date.strftime("%Y-%m-%d")
        params = {
            "latitude": city["lat"],
            "longitude": city["lon"],
            "hourly": "temperature_2m",
            "timezone": "auto",
            "temperature_unit": "fahrenheit" if unit == "fahrenheit" else "celsius",
            "start_date": date_str,
            "end_date": date_str,
        }
        r = requests.get(OPEN_METEO, params=params, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        times = data.get("hourly", {}).get("time") or []
        temps = data.get("hourly", {}).get("temperature_2m") or []
        if not temps:
            return None
        if times and len(times) == len(temps):
            temps = [t for t, ts in zip(temps, times) if ts.startswith(date_str)]
        if not temps:
            return None
        max_temp = max(temps)
        mean = sum(temps) / len(temps)
        variance = sum((t - mean) ** 2 for t in temps) / max(1, len(temps))
        sigma = math.sqrt(variance)
        sigma = max(1.0, sigma)
        return max_temp, sigma
    except Exception:
        return None


def _forecast_meteoblue(city: dict, unit: str, target_date: datetime) -> Optional[tuple[float, float]]:
    if not METEOBLUE_API_KEY:
        return None
    try:
        city = _resolve_city_coords(city) or city
        if city.get("lat") is None or city.get("lon") is None:
            return None
        date_str = target_date.strftime("%Y-%m-%d")
        params = {
            "apikey": METEOBLUE_API_KEY,
            "lat": city["lat"],
            "lon": city["lon"],
            "asl": city.get("asl", 0) or 0,
            "format": "json",
        }
        r = requests.get(METEOBLUE_BASIC_DAY, params=params, timeout=10)
        if not r.ok:
            return None
        data = r.json()
        day = data.get("data_day", {})
        times = day.get("time") or []
        tmax = day.get("temperature_max") or []
        tmin = day.get("temperature_min") or []
        tmean = day.get("temperature_mean") or []
        if not times or not tmax:
            return None
        try:
            idx = times.index(date_str)
        except ValueError:
            return None
        max_temp = float(tmax[idx])
        if unit == "fahrenheit":
            max_temp = (max_temp * 9 / 5) + 32
        min_temp = float(tmin[idx]) if idx < len(tmin) else None
        mean_temp = float(tmean[idx]) if idx < len(tmean) else None
        if unit == "fahrenheit":
            if min_temp is not None:
                min_temp = (min_temp * 9 / 5) + 32
            if mean_temp is not None:
                mean_temp = (mean_temp * 9 / 5) + 32
        if min_temp is not None:
            sigma = max(1.0, (max_temp - min_temp) / 4)
        elif mean_temp is not None:
            sigma = max(1.0, abs(max_temp - mean_temp) / 2)
        else:
            sigma = 1.5
        return max_temp, sigma
    except Exception:
        return None


def _forecast_max_and_sigma(city: dict, unit: str, target_date: datetime) -> Optional[dict]:
    sources = []
    om = _forecast_openmeteo(city, unit, target_date)
    if om:
        sources.append(om)
    mb = _forecast_meteoblue(city, unit, target_date)
    if mb:
        sources.append(mb)
    if not sources:
        return None
    max_avg = sum(s[0] for s in sources) / len(sources)
    sigma_model = sum(s[1] for s in sources) / len(sources)
    sigma_model = max(1.0, sigma_model)
    sigma_hist = _historical_sigma_openmeteo(city, unit, target_date)
    sigma_cal = sigma_model
    if sigma_hist:
        sigma_cal = (0.5 * sigma_model) + (0.5 * sigma_hist)
    if om and mb:
        model_diff = abs(om[0] - mb[0])
        sigma_cal += model_diff * 0.15
    sigma_cal = max(1.0, sigma_cal)
    skew = _skew_for_city_date(city, target_date)
    return {
        "max_temp": max_avg,
        "sigma": sigma_cal,
        "sigma_model": sigma_model,
        "sigma_hist": sigma_hist,
        "skew": skew,
    }


def _range_prob(low: Optional[float], high: Optional[float], max_temp: float, sigma: float, skew: float = 0.0) -> float:
    if low is not None and high is not None and low == high:
        low = low - 0.5
        high = high + 0.5
    if low is None and high is None:
        return 0.0
    cdf = (lambda x: _skew_cdf(x, max_temp, sigma, skew)) if abs(skew) > 0.001 else (lambda x: _norm_cdf(x, max_temp, sigma))
    if low is None:
        return cdf(high)
    if high is None:
        return 1.0 - cdf(low)
    return max(0.0, cdf(high) - cdf(low))


def _best_ev_outcome_for_market(market: dict, forecast) -> Optional[dict]:
    if isinstance(forecast, dict):
        max_temp = forecast.get("max_temp")
        sigma = forecast.get("sigma")
        skew = forecast.get("skew", 0.0)
    else:
        max_temp, sigma = forecast
        skew = 0.0
    outcomes, prices, token_ids = _parse_outcomes(market)
    if len(outcomes) < 2 or len(prices) < 2:
        return None
    title = market.get("groupItemTitle") or market.get("question") or ""
    low, high = _parse_range(title)
    if low is None and high is None:
        return None
    prob_yes = _range_prob(low, high, max_temp, sigma, skew)
    prob_no = 1.0 - prob_yes
    try:
        yes_idx = outcomes.index("Yes")
    except ValueError:
        yes_idx = 0
    try:
        no_idx = outcomes.index("No")
    except ValueError:
        no_idx = 1 if len(outcomes) > 1 else 0

    candidates = []
    if yes_idx < len(prices) and yes_idx < len(token_ids):
        yes_price = float(prices[yes_idx])
        candidates.append({
            "outcome": f"YES — {title}",
            "price": yes_price,
            "prob": prob_yes,
            "ev": prob_yes - yes_price,
            "token_id": token_ids[yes_idx],
            "market_slug": market.get("slug"),
            "max_temp": max_temp,
            "sigma": sigma,
            "skew": skew,
        })
    if no_idx < len(prices) and no_idx < len(token_ids):
        no_price = float(prices[no_idx])
        candidates.append({
            "outcome": f"NO — {title}",
            "price": no_price,
            "prob": prob_no,
            "ev": prob_no - no_price,
            "token_id": token_ids[no_idx],
            "market_slug": market.get("slug"),
            "max_temp": max_temp,
            "sigma": sigma,
            "skew": skew,
        })
    if not candidates:
        return None
    return max(candidates, key=lambda x: x["ev"])


def _normalize_team_name(name: str) -> str:
    t = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    stop = {"fc", "cf", "sc", "club", "team", "esports", "e-sports", "the"}
    parts = [p for p in t.split() if p and p not in stop]
    return " ".join(parts)


def _extract_teams(title: str) -> Optional[tuple[str, str]]:
    if not title:
        return None
    separators = [" vs. ", " vs ", " v ", " @ ", " at "]
    for sep in separators:
        if sep in title:
            left, right = title.split(sep, 1)
            return left.strip(), right.strip()
    return None


def _team_key(a: str, b: str) -> str:
    na = _normalize_team_name(a)
    nb = _normalize_team_name(b)
    return " | ".join(sorted([na, nb]))


def _team_tokens(name: str) -> list[str]:
    norm = _normalize_team_name(name)
    tokens = [t for t in norm.split() if t]
    replace = {"utd": "united", "u": "united", "st": "saint"}
    return [replace.get(t, t) for t in tokens]


def _team_tokens_match(a: str, b: str) -> bool:
    ta = set(_team_tokens(a))
    tb = set(_team_tokens(b))
    if not ta or not tb:
        return False
    return ta.issubset(tb) or tb.issubset(ta)


def _parse_total_line(text: str) -> Optional[float]:
    if not text:
        return None
    nums = re.findall(r"[-+]?[0-9]*\\.?[0-9]+", text)
    if not nums:
        return None
    try:
        return float(nums[-1])
    except Exception:
        return None


def _odds_to_prob(odds: Optional[str]) -> Optional[float]:
    if odds is None:
        return None
    try:
        val = float(odds)
    except Exception:
        return None
    if val == 0:
        return None
    # American odds
    if abs(val) >= 100:
        if val > 0:
            return 100.0 / (val + 100.0)
        return abs(val) / (abs(val) + 100.0)
    # Decimal odds
    if val >= 1.01:
        return 1.0 / val
    return None


def _odds_consensus_moneyline(odds_data: dict) -> Optional[dict]:
    bookies = odds_data.get("bookmakers") if odds_data else None
    if not bookies:
        return None
    home_probs = []
    away_probs = []
    draw_probs = []
    for markets in bookies.values():
        for m in markets:
            if m.get("name") not in ("ML", "Moneyline", "Match Winner", "Match Odds"):
                continue
            for o in m.get("odds", []):
                if "home" in o:
                    p = _odds_to_prob(o.get("home"))
                    if p is not None:
                        home_probs.append(p)
                if "away" in o:
                    p = _odds_to_prob(o.get("away"))
                    if p is not None:
                        away_probs.append(p)
                if "draw" in o:
                    p = _odds_to_prob(o.get("draw"))
                    if p is not None:
                        draw_probs.append(p)
            break
    if not home_probs and not away_probs:
        return None
    home = sum(home_probs) / len(home_probs) if home_probs else None
    away = sum(away_probs) / len(away_probs) if away_probs else None
    draw = sum(draw_probs) / len(draw_probs) if draw_probs else None
    return {"home": home, "away": away, "draw": draw}


def _odds_consensus_totals(odds_data: dict, line: float) -> Optional[dict]:
    bookies = odds_data.get("bookmakers") if odds_data else None
    if not bookies:
        return None
    over_probs = []
    under_probs = []
    best_line = None
    for markets in bookies.values():
        for m in markets:
            if m.get("name") != "Totals":
                continue
            # pick closest line
            closest = None
            for o in m.get("odds", []):
                hdp = o.get("hdp")
                if hdp is None:
                    continue
                diff = abs(float(hdp) - line)
                if closest is None or diff < closest[0]:
                    closest = (diff, o)
            if closest:
                o = closest[1]
                best_line = o.get("hdp", best_line)
                p_over = _odds_to_prob(o.get("over"))
                p_under = _odds_to_prob(o.get("under"))
                if p_over is not None:
                    over_probs.append(p_over)
                if p_under is not None:
                    under_probs.append(p_under)
            break
    if not over_probs and not under_probs:
        return None
    over = sum(over_probs) / len(over_probs) if over_probs else None
    under = sum(under_probs) / len(under_probs) if under_probs else None
    return {"over": over, "under": under, "line": best_line}


def _odds_consensus_spreads(odds_data: dict, line: float) -> Optional[dict]:
    bookies = odds_data.get("bookmakers") if odds_data else None
    if not bookies:
        return None
    home_probs = []
    away_probs = []
    for markets in bookies.values():
        best = None
        for m in markets:
            if m.get("name") != "Spread":
                continue
            for o in m.get("odds", []):
                try:
                    hdp = float(o.get("hdp"))
                except Exception:
                    continue
                diff = abs(abs(hdp) - abs(line))
                if best is None or diff < best[0]:
                    p_home = _odds_to_prob(o.get("home"))
                    p_away = _odds_to_prob(o.get("away"))
                    if p_home is None or p_away is None:
                        continue
                    best = (diff, p_home, p_away)
            break
        if best and best[0] <= 0.25:
            home_probs.append(best[1])
            away_probs.append(best[2])
    if not home_probs and not away_probs:
        return None
    home = sum(home_probs) / len(home_probs) if home_probs else None
    away = sum(away_probs) / len(away_probs) if away_probs else None
    return {"home": home, "away": away}


def _odds_consensus_btts(odds_data: dict) -> Optional[dict]:
    bookies = odds_data.get("bookmakers") if odds_data else None
    if not bookies:
        return None
    yes_probs = []
    no_probs = []
    for markets in bookies.values():
        for m in markets:
            if m.get("name") != "Both Teams To Score":
                continue
            for o in m.get("odds", []):
                p_yes = _odds_to_prob(o.get("yes"))
                p_no = _odds_to_prob(o.get("no"))
                if p_yes is not None:
                    yes_probs.append(p_yes)
                if p_no is not None:
                    no_probs.append(p_no)
            break
    if not yes_probs and not no_probs:
        return None
    yes = sum(yes_probs) / len(yes_probs) if yes_probs else None
    no = sum(no_probs) / len(no_probs) if no_probs else None
    return {"yes": yes, "no": no}


def _ev_level(ev: float) -> str:
    if ev >= 0.08:
        return "grande"
    if ev >= 0.04:
        return "moderada"
    return "pequena"


def _pace_label(total_line: Optional[float], category: str) -> str:
    if total_line is None:
        return "neutro"
    if category == "nba":
        if total_line >= 228:
            return "alto"
        if total_line <= 214:
            return "baixo"
        return "medio"
    if category == "soccer":
        if total_line >= 3:
            return "alto"
        if total_line <= 2:
            return "baixo"
        return "medio"
    return "neutro"


@app.get("/api/ev-clima/summary")
def ev_clima_summary(user: dict = Depends(get_current_user)):
    cached = _cache_get(_ev_clima_cache, "summary", 30 * 60)
    if cached is not None:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached
    tz = ZoneInfo("America/New_York")
    today_et = datetime.now(tz=tz)
    target_date = today_et + timedelta(days=1)
    items = []
    top_candidates = []
    for city in CLIMA_CITIES:
        slug = _build_clima_slug(city["slug"], target_date)
        event = _get_event_by_slug_gamma(slug)
        if not event:
            items.append({"city": city["name"], "slug": slug, "status": "not_found"})
            continue
        markets = event.get("markets") or []
        if not markets:
            items.append({"city": city["name"], "slug": slug, "status": "no_markets"})
            continue
        unit_texts = [(m.get("groupItemTitle") or m.get("question") or "") for m in markets]
        unit = _detect_unit(unit_texts, event.get("title") or "")
        forecast = _forecast_max_and_sigma(city, unit, target_date)
        if not forecast:
            items.append({"city": city["name"], "slug": slug, "status": "no_forecast"})
            continue
        candidates = []
        for m in markets:
            candidate = _best_ev_outcome_for_market(m, forecast)
            if candidate and candidate.get("ev", 0) > 0:
                price_val = float(candidate.get("price") or 0)
                if price_val < 0.20 or price_val > 0.97:
                    continue
                explanation = (
                    f"Max forecast {candidate['max_temp']:.1f} ({unit}) | sigma {candidate['sigma']:.1f} | "
                    f"prob {candidate['prob']*100:.1f}% | EV {candidate['ev']*100:.1f}% | skew {candidate.get('skew', 0):+.2f}"
                )
                candidate = dict(candidate)
                candidate["explanation"] = explanation
                candidate["link"] = f"https://polymarket.com/market/{candidate.get('market_slug')}"
                candidates.append(candidate)
        if not candidates:
            items.append({"city": city["name"], "slug": slug, "status": "no_ev"})
            continue
        candidates.sort(key=lambda c: c["ev"], reverse=True)
        top3 = candidates[:3]
        items.append({
            "city": city["name"],
            "slug": slug,
            "status": "ok",
            "bets": [
                {
                    "outcome": c["outcome"],
                    "price": c["price"],
                    "prob": c["prob"],
                    "ev": c["ev"],
                    "sigma": c["sigma"],
                    "token_id": c["token_id"],
                    "explanation": c["explanation"],
                    "link": c["link"],
                }
                for c in top3
            ],
        })
        for c in candidates:
            top_candidates.append({
                "city": city["name"],
                "outcome": c["outcome"],
                "price": c["price"],
                "prob": c["prob"],
                "ev": c["ev"],
                "sigma": c["sigma"],
                "token_id": c["token_id"],
                "explanation": c["explanation"],
                "link": c["link"],
            })
    top5 = []
    seen_cities = set()
    for c in sorted(top_candidates, key=lambda c: (c["sigma"], -c["prob"])):
        city_key = (c.get("city") or "").strip().lower()
        if not city_key or city_key in seen_cities:
            continue
        seen_cities.add(city_key)
        top5.append(c)
        if len(top5) >= 5:
            break
    payload = {"items": items, "top5": top5, "updated_at": datetime.now(timezone.utc).isoformat()}
    _cache_set(_ev_clima_cache, "summary", payload)
    return payload


def _gamma_sport_codes_by_category(category: str) -> list[str]:
    sports = _gamma_get_sports()
    if category == "nba":
        return ["nba"]
    if category == "soccer":
        soccer_codes = []
        soccer_markers = [
            "premierleague",
            "laliga",
            "uefa",
            "fifa",
            "mls",
            "seriea",
            "bundesliga",
            "ligue",
            "conmebol",
            "copa",
            "afc",
            "concacaf",
        ]
        for s in sports:
            res = (s.get("resolution") or "").lower()
            if any(m in res for m in soccer_markers):
                soccer_codes.append(s.get("sport"))
        return sorted({c for c in soccer_codes if c})
    if category == "esports":
        esport_codes = []
        esport_markers = ["hltv", "lolesports", "vlr.gg", "liquipedia", "esports", "dota"]
        for s in sports:
            res = (s.get("resolution") or "").lower()
            if any(m in res for m in esport_markers):
                esport_codes.append(s.get("sport"))
        # fallback comum
        esport_codes.extend(["cs2", "valorant", "lol", "dota2"])
        return sorted({c for c in esport_codes if c})
    return []


def _gamma_upcoming_events_for_codes(codes: list[str], limit: int = 20) -> list[dict]:
    events: list[dict] = []
    now = datetime.now(timezone.utc)
    max_date = now + timedelta(days=7)
    for code in codes:
        sport_entry = next((s for s in _gamma_get_sports() if s.get("sport") == code), None)
        if not sport_entry:
            continue
        series_id = str(sport_entry.get("series") or "")
        if not series_id:
            continue
        series = _gamma_get_series(series_id)
        if not series:
            continue
        for ev in series.get("events", []):
            if not ev or ev.get("closed"):
                continue
            event_date = ev.get("eventDate")
            start_time = ev.get("startTime") or ev.get("gameStartTime")
            dt = None
            if start_time:
                try:
                    dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                except Exception:
                    dt = None
            elif event_date:
                try:
                    dt = datetime.fromisoformat(event_date + "T00:00:00+00:00")
                except Exception:
                    dt = None
            if dt and dt > max_date:
                continue
            events.append(ev)
            if len(events) >= limit:
                return events
    return events


def _odds_events_index(sport: str, leagues: Optional[list[str]] = None) -> dict[str, list[dict]]:
    events: list[dict] = []
    if leagues:
        for league in leagues:
            data = _odds_get("/events", {"sport": sport, "league": league, "status": "pending", "limit": 200})
            if isinstance(data, list):
                events.extend(data)
    else:
        data = _odds_get("/events", {"sport": sport, "status": "pending", "limit": 200})
        if isinstance(data, list):
            events.extend(data)
    index: dict[str, list[dict]] = {}
    for ev in events:
        home = ev.get("home")
        away = ev.get("away")
        if not home or not away:
            continue
        key = _team_key(home, away)
        index.setdefault(key, []).append(ev)
    return index


def _match_odds_event(index: dict[str, list[dict]], team_a: str, team_b: str, start_time: Optional[str]) -> Optional[dict]:
    key = _team_key(team_a, team_b)
    candidates = index.get(key, [])
    if not candidates:
        all_events: list[dict] = []
        for lst in index.values():
            all_events.extend(lst)
        for ev in all_events:
            home = ev.get("home") or ""
            away = ev.get("away") or ""
            if (_team_tokens_match(team_a, home) and _team_tokens_match(team_b, away)) or (
                _team_tokens_match(team_a, away) and _team_tokens_match(team_b, home)
            ):
                candidates.append(ev)
        if not candidates:
            return None
    if not start_time:
        return candidates[0]
    try:
        target = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    except Exception:
        return candidates[0]
    best = None
    for ev in candidates:
        try:
            dt = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00"))
        except Exception:
            dt = None
        if not dt:
            continue
        diff = abs((dt - target).total_seconds())
        if best is None or diff < best[0]:
            best = (diff, ev)
    return best[1] if best else candidates[0]


def _build_explanation(category: str, outcome: str, prob_impl: float, prob_real: float, ev: float, total_line: Optional[float]) -> str:
    adv = _ev_level(ev)
    pace = _pace_label(total_line, category)
    impl = prob_impl * 100
    real = prob_real * 100
    if category == "nba":
        return (
            f"Vantagem real {adv} (baseada em odds consenso). Ritmo esperado: {pace}"
            + (f" (linha {total_line}). " if total_line else ". ")
            + f"Prob. implicita {impl:.1f}% vs real {real:.1f}%."
        )
    if category == "soccer":
        return (
            f"Vantagem real {adv}. Ritmo esperado: {pace}"
            + (f" (linha {total_line}). " if total_line else ". ")
            + f"Prob. implicita {impl:.1f}% vs real {real:.1f}%."
        )
    return (
        f"Vantagem real {adv} (odds consenso). "
        f"Prob. implicita {impl:.1f}% vs real {real:.1f}%."
    )


@app.get("/api/ev-esportes/summary")
def ev_esportes_summary(user: dict = Depends(get_current_user)):
    cached_any = _cache_peek(_ev_esportes_cache, "summary")
    cached = _cache_get(_ev_esportes_cache, "summary", 20 * 60)
    if cached is not None:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached
    categories = [
        {"id": "nba", "label": "NBA", "sport": "basketball", "leagues": ["usa-nba"]},
        {"id": "soccer", "label": "Futebol", "sport": "football", "leagues": [
            "england-premier-league",
            "spain-la-liga",
            "italy-serie-a",
            "germany-bundesliga",
            "france-ligue-1",
            "international-clubs-uefa-champions-league",
            "international-clubs-uefa-europa-league",
            "international-clubs-uefa-conference-league",
            "international-clubs-copa-libertadores",
            "brazil-brasileiro-serie-a",
            "usa-mls",
        ]},
        {"id": "esports", "label": "E-sports", "sport": "esports", "leagues": None},
    ]

    result = []
    remaining_budget = 20
    used_requests = 0
    limit_reached = False
    for cat in categories:
        odds_index = _odds_events_index(cat["sport"], cat["leagues"])
        codes = _gamma_sport_codes_by_category(cat["id"])
        events = _gamma_upcoming_events_for_codes(codes, limit=20)
        games = []
        for ev in events:
            if remaining_budget <= 0:
                limit_reached = True
                break
            title = ev.get("title") or ev.get("ticker") or ""
            teams = _extract_teams(title)
            if not teams:
                continue
            team_a, team_b = teams
            odds_ev = _match_odds_event(odds_index, team_a, team_b, ev.get("startTime") or ev.get("gameStartTime"))
            if not odds_ev:
                continue
            odds_data = _odds_get("/odds", {"eventId": odds_ev.get("id"), "bookmakers": ODDS_API_IO_BOOKMAKERS})
            if not odds_data:
                continue
            remaining_budget -= 1
            used_requests += 1

            event_detail = _get_event_by_slug_gamma(ev.get("slug"))
            if not event_detail:
                continue

            bets = []
            for market in event_detail.get("markets", []):
                if not market.get("active") or market.get("closed"):
                    continue
                outcomes, prices, token_ids = _parse_outcomes(market)
                if not outcomes or not prices:
                    continue
                smt = market.get("sportsMarketType")
                if smt in ("moneyline", None):
                    probs = _odds_consensus_moneyline(odds_data)
                    if not probs:
                        continue
                    for i, outcome in enumerate(outcomes):
                        price = float(prices[i]) if i < len(prices) else None
                        if price is None:
                            continue
                        outcome_norm = _normalize_team_name(outcome)
                        home_norm = _normalize_team_name(odds_ev.get("home", ""))
                        away_norm = _normalize_team_name(odds_ev.get("away", ""))
                        if outcome_norm == home_norm:
                            prob_real = probs.get("home")
                        elif outcome_norm == away_norm:
                            prob_real = probs.get("away")
                        elif outcome.lower() in ("draw", "tie"):
                            prob_real = probs.get("draw")
                        else:
                            prob_real = None
                        if prob_real is None:
                            continue
                        ev_value = prob_real - price
                        if ev_value <= 0:
                            continue
                        bets.append({
                            "outcome": outcome,
                            "price": price,
                            "prob_real": prob_real,
                            "prob_impl": price,
                            "ev": ev_value,
                            "token_id": token_ids[i] if i < len(token_ids) else None,
                            "market_slug": market.get("slug"),
                            "explanation": _build_explanation(cat["id"], outcome, price, prob_real, ev_value, None),
                        })
                elif smt == "totals":
                    line = market.get("line")
                    if line is None:
                        line = _parse_total_line(market.get("groupItemTitle") or market.get("question") or "")
                    if line is None:
                        continue
                    probs = _odds_consensus_totals(odds_data, line)
                    if not probs:
                        continue
                    for i, outcome in enumerate(outcomes):
                        price = float(prices[i]) if i < len(prices) else None
                        if price is None:
                            continue
                        if outcome.lower().startswith("over"):
                            prob_real = probs.get("over")
                        elif outcome.lower().startswith("under"):
                            prob_real = probs.get("under")
                        else:
                            prob_real = None
                        if prob_real is None:
                            continue
                        ev_value = prob_real - price
                        if ev_value <= 0:
                            continue
                        bets.append({
                            "outcome": outcome,
                            "price": price,
                            "prob_real": prob_real,
                            "prob_impl": price,
                            "ev": ev_value,
                            "token_id": token_ids[i] if i < len(token_ids) else None,
                            "market_slug": market.get("slug"),
                            "explanation": _build_explanation(cat["id"], outcome, price, prob_real, ev_value, line),
                        })
                elif smt == "spreads":
                    line = market.get("line")
                    if line is None:
                        line = _parse_total_line(market.get("groupItemTitle") or market.get("question") or "")
                    if line is None:
                        continue
                    probs = _odds_consensus_spreads(odds_data, float(line))
                    if not probs:
                        continue
                    home_norm = _normalize_team_name(odds_ev.get("home", ""))
                    away_norm = _normalize_team_name(odds_ev.get("away", ""))
                    for i, outcome in enumerate(outcomes):
                        price = float(prices[i]) if i < len(prices) else None
                        if price is None:
                            continue
                        outcome_norm = _normalize_team_name(outcome)
                        if outcome_norm == home_norm:
                            prob_real = probs.get("home")
                        elif outcome_norm == away_norm:
                            prob_real = probs.get("away")
                        else:
                            prob_real = None
                        if prob_real is None:
                            continue
                        ev_value = prob_real - price
                        if ev_value <= 0:
                            continue
                        bets.append({
                            "outcome": outcome,
                            "price": price,
                            "prob_real": prob_real,
                            "prob_impl": price,
                            "ev": ev_value,
                            "token_id": token_ids[i] if i < len(token_ids) else None,
                            "market_slug": market.get("slug"),
                            "explanation": _build_explanation(cat["id"], outcome, price, prob_real, ev_value, None),
                        })
                elif smt == "both_teams_to_score":
                    probs = _odds_consensus_btts(odds_data)
                    if not probs:
                        continue
                    for i, outcome in enumerate(outcomes):
                        price = float(prices[i]) if i < len(prices) else None
                        if price is None:
                            continue
                        low = outcome.strip().lower()
                        if low.startswith("yes"):
                            prob_real = probs.get("yes")
                        elif low.startswith("no"):
                            prob_real = probs.get("no")
                        else:
                            prob_real = None
                        if prob_real is None:
                            continue
                        ev_value = prob_real - price
                        if ev_value <= 0:
                            continue
                        bets.append({
                            "outcome": outcome,
                            "price": price,
                            "prob_real": prob_real,
                            "prob_impl": price,
                            "ev": ev_value,
                            "token_id": token_ids[i] if i < len(token_ids) else None,
                            "market_slug": market.get("slug"),
                            "explanation": _build_explanation(cat["id"], outcome, price, prob_real, ev_value, None),
                        })

            bets = sorted(bets, key=lambda x: x["ev"], reverse=True)[:5]
            if not bets:
                continue
            games.append({
                "title": title,
                "slug": ev.get("slug"),
                "start_time": ev.get("startTime") or ev.get("gameStartTime"),
                "bets": bets,
            })

        result.append({"id": cat["id"], "label": cat["label"], "games": games})
    payload = {
        "categories": result,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "requests_used": used_requests,
        "requests_limit": 20,
        "limit_reached": limit_reached,
    }
    if not result and cached_any is not None:
        cached_any = dict(cached_any)
        cached_any["from_cache"] = True
        cached_any["stale_reason"] = "limit_or_error"
        return cached_any
    _cache_set(_ev_esportes_cache, "summary", payload)
    return payload


class EvClimaBuyRequest(BaseModel):
    token_id: str
    amount: Optional[float] = None


class EvEsportesBuyRequest(BaseModel):
    token_id: str
    amount: Optional[float] = None


@app.post("/api/ev-clima/buy")
def ev_clima_buy(req: EvClimaBuyRequest, user: dict = Depends(get_current_user)):
    row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        raise HTTPException(status_code=400, detail="Config não encontrada.")
    key = (row.get("private_key") or "").strip()
    if not key or key == "0x...":
        raise HTTPException(status_code=400, detail="Salve a chave privada na Config primeiro.")
    api_key = (row.get("api_key") or "").strip()
    api_secret = (row.get("api_secret") or "").strip()
    api_passphrase = (row.get("api_passphrase") or "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise HTTPException(status_code=400, detail="Salve as credenciais API da Polymarket na Config.")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    sig_type = int(row.get("signature_type") or 0)
    funder = (row.get("funder_address") or "").strip()
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        signature_type=sig_type,
        funder=funder or None,
    )
    client.set_api_creds(api_key, api_secret, api_passphrase)

    min_bet = float(row.get("min_bet", 5))
    safe_bet = row.get("safe_bet")
    amount = float(req.amount) if req.amount is not None else (float(safe_bet) if safe_bet else min_bet)
    amount = max(amount, min_bet)

    mo = MarketOrderArgs(
        token_id=req.token_id,
        amount=amount,
        side=BUY,
        price=float(row.get("max_token_price", 0.95)),
        order_type=OrderType.FOK,
    )
    try:
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        ok = resp.get("status") in ("matched", "live")
        return {"ok": ok, "status": resp.get("status")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao enviar ordem: {e!s}")


@app.post("/api/ev-esportes/buy")
def ev_esportes_buy(req: EvEsportesBuyRequest, user: dict = Depends(get_current_user)):
    row = _config_from_supabase(user["id"], user["_token"])
    if not row:
        raise HTTPException(status_code=400, detail="Config não encontrada.")
    key = (row.get("private_key") or "").strip()
    if not key or key == "0x...":
        raise HTTPException(status_code=400, detail="Salve a chave privada na Config primeiro.")
    api_key = (row.get("api_key") or "").strip()
    api_secret = (row.get("api_secret") or "").strip()
    api_passphrase = (row.get("api_passphrase") or "").strip()
    if not api_key or not api_secret or not api_passphrase:
        raise HTTPException(status_code=400, detail="Salve as credenciais API da Polymarket na Config.")

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    sig_type = int(row.get("signature_type") or 0)
    funder = (row.get("funder_address") or "").strip()
    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=key,
        signature_type=sig_type,
        funder=funder or None,
    )
    client.set_api_creds(api_key, api_secret, api_passphrase)

    min_bet = float(row.get("min_bet", 5))
    safe_bet = row.get("safe_bet")
    amount = float(req.amount) if req.amount is not None else (float(safe_bet) if safe_bet else min_bet)
    amount = max(amount, min_bet)

    mo = MarketOrderArgs(
        token_id=req.token_id,
        amount=amount,
        side=BUY,
        price=float(row.get("max_token_price", 0.95)),
        order_type=OrderType.FOK,
    )
    try:
        signed = client.create_market_order(mo)
        resp = client.post_order(signed, OrderType.FOK)
        ok = resp.get("status") in ("matched", "live")
        return {"ok": ok, "status": resp.get("status")}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Falha ao enviar ordem: {e!s}")


@app.get("/api/admin/users")
def admin_list_users(_admin: dict = Depends(require_admin)):
    """Lista todos os usuários (user_config) para o admin. Só malagueta.canal@gmail.com."""
    try:
        rows = _admin_get_all_user_configs()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao listar usuários: {e!s}")
    return {"users": rows}


@app.post("/api/admin/grant-access")
def admin_grant_access(body: AdminGrantAccessRequest, _admin: dict = Depends(require_admin)):
    """Libera 30 dias de acesso para um usuário (por user_id ou email). Só admin."""
    user_id = body.user_id
    if not user_id and body.email:
        try:
            all_rows = _admin_get_all_user_configs()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro ao buscar usuário: {e!s}")
        for r in all_rows:
            if (r.get("email") or "").strip().lower() == (body.email or "").strip().lower():
                user_id = r.get("user_id")
                break
        if not user_id:
            raise HTTPException(status_code=404, detail="Nenhum usuário encontrado com esse e-mail.")
    if not user_id:
        raise HTTPException(status_code=400, detail="Informe user_id ou email.")
    try:
        _admin_grant_days(user_id, 30)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao liberar acesso: {e!s}")
    return {"ok": True, "message": "30 dias de acesso liberados.", "user_id": user_id}


FRONTEND_DIR = PROJECT_ROOT / "frontend"


@app.get("/api/public-config")
def public_config():
    """Configuração pública para o frontend (Supabase URL e anon key para Auth)."""
    return {"supabase_url": SUPABASE_URL, "supabase_anon_key": SUPABASE_ANON_KEY}


@app.get("/")
def index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file, media_type="text/html")
    return {"message": "Dashboard API. Monte o frontend em frontend/index.html e acesse /"}


@app.get("/{path:path}")
def frontend(path: str):
    """Serve arquivos estáticos do frontend; bloqueia path traversal."""
    base = FRONTEND_DIR.resolve()
    f = (base / path).resolve()
    try:
        f.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found")
    if not f.is_file():
        if (FRONTEND_DIR / "index.html").exists():
            return FileResponse(FRONTEND_DIR / "index.html", media_type="text/html")
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(f)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
