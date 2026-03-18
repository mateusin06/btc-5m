#!/usr/bin/env python3
"""
Backend da dashboard do bot Polymarket.
API para config (Supabase), derivar credenciais, start/stop do bot e estatísticas.
Requer login; config e trades são por usuário.
"""

import json
import os
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

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://thkvxvdjcxunitxpeivg.supabase.co").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRoa3Z4dmRqY3h1bml0eHBlaXZnIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzIzNzg3NDEsImV4cCI6MjA4Nzk1NDc0MX0.znZAXuiFZaU1R_6h6TYBXd-765pgoxmbditxRXrmHN8")
ADMIN_EMAIL = "malagueta.canal@gmail.com"
SUPABASE_SERVICE_ROLE_KEY = (
    os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRoa3Z4dmRqY3h1bml0eHBlaXZnIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjM3ODc0MSwiZXhwIjoyMDg3OTU0NzQxfQ.5lJAxLoDkuINpkBmbM3iiJmK3wSnqHZA6ZxdcE6hDkI"
)
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


def _config_to_supabase(user_id: str, token: str, data: dict, email: Optional[str] = None) -> None:
    try:
        payload = {k: v for k, v in data.items() if v is not None}
        payload["user_id"] = user_id
        if email is not None:
            payload["email"] = email
        existing = _config_from_supabase(user_id, token)
        if existing:
            r = requests.patch(
                f"{SUPABASE_URL}/rest/v1/user_config",
                params={"user_id": f"eq.{user_id}"},
                headers=_supabase_headers(token),
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
        else:
            r = requests.post(
                f"{SUPABASE_URL}/rest/v1/user_config",
                headers=_supabase_headers(token),
                json=payload,
                timeout=10,
            )
            r.raise_for_status()
    except requests.exceptions.HTTPError as e:
        raise HTTPException(status_code=500, detail="Erro ao salvar config. Tente novamente.")


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
    existing = _config_from_supabase(user_id, token)
    if existing:
        return
    trial_end = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/user_config",
            headers={**_supabase_headers(token), "Content-Type": "application/json"},
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
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/user_config",
        params={"user_id": f"eq.{user_id}", "select": "subscription_ends_at"},
        headers=_admin_headers(),
        timeout=10,
    )
    r.raise_for_status()
    rows = r.json() if r.json() else []
    if not rows:
        raise HTTPException(status_code=404, detail="Usuário não encontrado em user_config.")
    now = datetime.now(timezone.utc)
    current_end = _parse_iso_date(rows[0].get("subscription_ends_at"))
    if current_end and current_end > now:
        new_end = current_end + timedelta(days=add_days)
    else:
        new_end = now + timedelta(days=add_days)
    patch_r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/user_config",
        params={"user_id": f"eq.{user_id}"},
        headers=_admin_headers(),
        json={"subscription_ends_at": new_end.isoformat()},
        timeout=10,
    )
    patch_r.raise_for_status()


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
    starting_bankroll: Optional[float] = None
    min_bet: Optional[float] = None
    bot_mode: Optional[Literal["safe", "spike_ai", "moon", "aggressive", "degen", "arbitragem", "arb_kalshi", "only_hedge_plus", "odd_master", "90_95"]] = None
    aggressive_bet_pct: Optional[float] = None
    max_token_price: Optional[float] = None
    arb_min_profit_pct: Optional[float] = None
    safe_bet: Optional[float] = None
    only_hedge_bet: Optional[float] = None
    odd_master_bet: Optional[float] = None
    bet_90_95: Optional[float] = None
    arbitragem_pct: Optional[float] = None
    use_chainlink_open: Optional[bool] = None
    max_delta_open_usd: Optional[float] = None


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
    use_chainlink_open: bool = True
    max_delta_open_usd: float = 0.0
    has_private_key: bool
    has_api_creds: bool
    has_kalshi_api_creds: bool
    access_ok: bool = True
    access_reason: Optional[str] = None
    trial_ends_at: Optional[str] = None
    subscription_ends_at: Optional[str] = None
    payment_wallet: Optional[str] = None


class BotStartRequest(BaseModel):
    mode: Literal["safe", "spike_ai", "moon", "aggressive", "dry_run", "arbitragem", "arb_kalshi", "only_hedge_plus", "odd_master", "90_95"] = Field(..., description="Modo de trading")
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
            use_chainlink_open=True,
            max_delta_open_usd=0.0,
            has_private_key=False,
            has_api_creds=False,
            has_kalshi_api_creds=False,
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
        use_chainlink_open=bool(row.get("use_chainlink_open") if row.get("use_chainlink_open") is not None else True),
        max_delta_open_usd=float(row.get("max_delta_open_usd") or 0),
        has_private_key=bool(row.get("private_key") and str(row.get("private_key", "")).strip() and row.get("private_key") != "0x..."),
        has_api_creds=bool(row.get("api_key") and row.get("api_secret") and row.get("api_passphrase")),
        has_kalshi_api_creds=bool(row.get("kalshi_api_key") and row.get("kalshi_api_secret")),
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
    if upd.use_chainlink_open is not None:
        data["use_chainlink_open"] = bool(upd.use_chainlink_open)
    if upd.max_delta_open_usd is not None:
        data["max_delta_open_usd"] = max(0.0, float(upd.max_delta_open_usd))
    _config_to_supabase(user["id"], user["_token"], data, user.get("email"))
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
    if mode == "arb_kalshi":
        markets_list = [m for m in markets_list if m in ("btc15m", "eth15m")]
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
        if mode in ("safe", "spike_ai", "moon"):
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
        if mode in ("arbitragem", "arb_kalshi"):
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
    _use_cl = row.get("use_chainlink_open")
    env["USE_CHAINLINK_OPEN"] = "1" if (_use_cl is None or _use_cl) else "0"
    env["MAX_DELTA_OPEN_USD"] = str(max(0.0, float(row.get("max_delta_open_usd") or 0)))
    if mode == "arb_kalshi":
        env["KALSHI_API_KEY_ID"] = row.get("kalshi_api_key", "")
        env["KALSHI_PRIVATE_KEY_PEM"] = row.get("kalshi_api_secret", "")
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

    # Atualizar Supabase com o modo (e parâmetros) usados ao iniciar — assim bot_mode fica sincronizado
    try:
        start_config: dict = {"bot_mode": mode}
        if mode in ("safe", "spike_ai", "moon") and (req.safe_bet is not None or (row.get("safe_bet") and float(row["safe_bet"]))):
            start_config["safe_bet"] = req.safe_bet if req.safe_bet is not None else float(row["safe_bet"])
        if mode == "aggressive":
            start_config["aggressive_bet_pct"] = int(round(pct_aggressive))
        if mode == "only_hedge_plus" and (req.only_hedge_bet is not None or (row.get("only_hedge_bet") and float(row["only_hedge_bet"]))):
            start_config["only_hedge_bet"] = req.only_hedge_bet if req.only_hedge_bet is not None else float(row["only_hedge_bet"])
        if mode == "odd_master" and (req.odd_master_bet is not None or (row.get("odd_master_bet") and float(row["odd_master_bet"]))):
            start_config["odd_master_bet"] = req.odd_master_bet if req.odd_master_bet is not None else float(row["odd_master_bet"])
        if mode == "90_95" and (req.bet_90_95 is not None or (row.get("bet_90_95") and float(row["bet_90_95"]))):
            start_config["bet_90_95"] = req.bet_90_95 if req.bet_90_95 is not None else float(row["bet_90_95"])
        if mode in ("arbitragem", "arb_kalshi") and (req.arbitragem_pct is not None or (row.get("arbitragem_pct") and float(row["arbitragem_pct"]))):
            start_config["arbitragem_pct"] = int(req.arbitragem_pct if req.arbitragem_pct is not None else float(row["arbitragem_pct"]))
        _config_to_supabase(user_id, user["_token"], start_config, user.get("email"))
    except Exception:
        pass  # não falhar o start se o update da config falhar

    cmd = [sys.executable, str(PROJECT_ROOT / "bot.py"), "--mode", mode, "--markets", ",".join(markets_list)]
    if dry_run:
        cmd.append("--dry-run")
    if mode in ("safe", "spike_ai", "moon"):
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
    if mode in ("arbitragem", "arb_kalshi"):
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
        elif mode in ("arbitragem", "arb_kalshi") and (req.arbitragem_pct is not None or (row.get("arbitragem_pct") and float(row["arbitragem_pct"]))):
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
