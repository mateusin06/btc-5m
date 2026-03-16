import json
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import requests


@dataclass
class AIDecision:
    allow: bool
    direction: Optional[str] = None  # "up" | "down" | None
    confidence: float = 0.0
    reason: str = ""
    raw: Any = None


_JSON_RE = re.compile(r"\{[\s\S]*\}")


def _extract_json_obj(text: str) -> Optional[dict]:
    if not text:
        return None
    m = _JSON_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def ask_ollama_trade_gate(
    *,
    market: str,
    side: str,
    seconds_to_close: int,
    token_price: Optional[float],
    ta_details: dict,
    score: float,
    confidence: float,
    window_open: float,
    current_price: float,
    mode: str,
) -> AIDecision:
    """
    Consulta o Ollama (local) para aprovar ou negar uma entrada.

    Requer Ollama rodando em OLLAMA_HOST (default http://localhost:11434).
    Modelo em OLLAMA_MODEL (default qwen2.5:3b).
    Timeout em OLLAMA_TIMEOUT_SEC (default 2.0).
    """
    host = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
    model = (os.getenv("OLLAMA_MODEL") or "qwen2.5:3b").strip()
    timeout_s = float(os.getenv("OLLAMA_TIMEOUT_SEC", "2.0") or "2.0")

    system = (
        "Você é um analista de trades de curtíssimo prazo. "
        "Responda SOMENTE com JSON válido, sem markdown, sem texto extra."
    )
    user = {
        "mode": mode,
        "market": market,
        "candidate_side": side,
        "seconds_to_close": int(seconds_to_close),
        "token_price": token_price,
        "ta": ta_details,
        "score": float(score),
        "confidence": float(confidence),
        "window_open": float(window_open),
        "current_price": float(current_price),
        "rules": {
            "output_schema": {
                "allow": "boolean",
                "direction": "\"up\"|\"down\" (opcional)",
                "confidence": "0..1 (opcional)",
                "reason": "string curta (opcional)",
            }
        },
    }

    payload = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Decida se devemos executar este trade agora. "
                    "Se houver sinais conflitantes, incerteza alta, ou dados insuficientes, negue.\n\n"
                    "Entrada:\n"
                    f"{json.dumps(user, ensure_ascii=False)}\n\n"
                    "Responda no esquema:\n"
                    "{\"allow\":true/false,\"direction\":\"up|down\",\"confidence\":0.0-1.0,\"reason\":\"...\"}"
                ),
            },
        ],
    }

    try:
        r = requests.post(f"{host}/api/chat", json=payload, timeout=timeout_s)
        r.raise_for_status()
        data = r.json()
        content = (
            (((data or {}).get("message") or {}).get("content"))
            or (((data or {}).get("messages") or [{}])[-1].get("content"))
            or ""
        )
        obj = _extract_json_obj(content) if isinstance(content, str) else (content if isinstance(content, dict) else None)
        if not isinstance(obj, dict):
            return AIDecision(allow=False, reason="IA: resposta inválida (sem JSON).", raw=data)
        allow = bool(obj.get("allow", False))
        direction = obj.get("direction")
        if direction not in ("up", "down"):
            direction = None
        try:
            conf_out = float(obj.get("confidence", 0.0) or 0.0)
        except Exception:
            conf_out = 0.0
        conf_out = max(0.0, min(1.0, conf_out))
        reason = str(obj.get("reason", "") or "")
        return AIDecision(allow=allow, direction=direction, confidence=conf_out, reason=reason, raw=data)
    except Exception as e:
        return AIDecision(allow=False, reason=f"IA indisponível/erro: {e}", raw=None)

