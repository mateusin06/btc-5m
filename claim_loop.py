#!/usr/bin/env python3
"""
Loop de claim por API para um usuário.
Roda em subprocess; lê credenciais de POLY_* no env e executa claim a cada CLAIM_INTERVAL_SEC.
Log em data/autoclaim_<BOT_USER_ID>.txt.
"""

import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

# Não sobrescrever POLY_* que a dashboard passou ao iniciar o processo (credenciais do Supabase)
load_dotenv(override=False)

# Intervalo entre execuções (segundos)
CLAIM_INTERVAL_SEC = int(os.getenv("CLAIM_INTERVAL_SEC", "60"))


def _safe_user_id(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return "default"
    return re.sub(r"[^\w\-]", "", raw)[:64] or "default"


def main() -> None:
    user_id = _safe_user_id(os.getenv("BOT_USER_ID", ""))
    log_dir = Path(__file__).resolve().parent / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"autoclaim_{user_id}.txt"

    def log(msg: str) -> None:
        print(msg)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    try:
        from claim_api import run_claim
    except ImportError:
        log("Erro: módulo claim_api não encontrado.")
        sys.exit(1)

    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    try:
        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))
    except ValueError:
        sig_type = 1

    if not key:
        log("Erro: POLY_PRIVATE_KEY não definido no ambiente.")
        sys.exit(1)

    log("Claim por API iniciado (intervalo %s s)." % CLAIM_INTERVAL_SEC)
    while True:
        try:
            result = run_claim(
                private_key=key,
                funder_address=funder,
                signature_type=sig_type,
            )
            log(result.get("message", ""))
            for d in result.get("details", []):
                log("  %s" % d)
            if not result.get("ok") and result.get("error"):
                log("  Erro: %s" % result["error"])
        except Exception as e:
            log("Exceção: %s" % e)
        time.sleep(CLAIM_INTERVAL_SEC)


if __name__ == "__main__":
    main()
