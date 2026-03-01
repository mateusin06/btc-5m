#!/usr/bin/env python3
"""
Claim de posições redeemable na Polymarket via API (polymarket-apis).

Usa PolymarketDataClient para listar posições e PolymarketWeb3Client para
redeem. Funciona com EOA (0), Magic/Email (1) e Safe/Proxy (2).
Requer: pip install polymarket-apis (Python >= 3.12).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Normalizar para int 0/1/2
def _norm_sig_type(v: Any) -> int:
    if v is None:
        return 1
    try:
        n = int(v)
        return n if n in (0, 1, 2) else 1
    except (TypeError, ValueError):
        return 1


def run_claim(
    private_key: str,
    funder_address: str,
    signature_type: int | None = 1,
) -> dict[str, Any]:
    """
    Busca posições redeemable do usuário e executa redeem de todas.
    Usa polymarket-apis (PolymarketDataClient + PolymarketWeb3Client).

    Args:
        private_key: Chave privada (com ou sem 0x).
        funder_address: Endereço da carteira/portfolio (proxy para Magic/Safe).
        signature_type: 0=EOA, 1=Magic/Email, 2=Safe/Proxy.

    Returns:
        {
            "ok": bool,
            "redeemed": int,
            "failed": int,
            "total_value": float,
            "message": str,
            "details": list[str],
            "error": str | None  # se ok=False e falha antes de tentar posições
        }
    """
    sig_type = _norm_sig_type(signature_type)
    key = (private_key or "").strip()
    funder = (funder_address or "").strip()

    if not key or key == "0x...":
        return {
            "ok": False,
            "redeemed": 0,
            "failed": 0,
            "total_value": 0.0,
            "message": "Chave privada não configurada.",
            "details": [],
            "error": "missing_private_key",
        }

    try:
        from polymarket_apis import PolymarketDataClient, PolymarketWeb3Client
    except ImportError as e:
        return {
            "ok": False,
            "redeemed": 0,
            "failed": 0,
            "total_value": 0.0,
            "message": "Instale polymarket-apis (pip install polymarket-apis). Requer Python >= 3.12.",
            "details": [],
            "error": str(e),
        }

    details: list[str] = []
    redeemed = 0
    failed = 0
    total_value = 0.0

    try:
        # PolymarketWeb3Client não aceita funder; o endereço (EOA/proxy/Safe) é derivado da chave
        web3 = PolymarketWeb3Client(
            private_key=key,
            signature_type=sig_type,
        )

        # Endereço cujas posições vamos buscar (proxy/Safe já é o web3.address quando sig_type 1 ou 2)
        user_address = web3.address

        data_client = PolymarketDataClient()
        positions = data_client.get_positions(
            user=user_address,
            redeemable=True,
            size_threshold=0.0,
        )

        if not positions:
            return {
                "ok": True,
                "redeemed": 0,
                "failed": 0,
                "total_value": 0.0,
                "message": "Nenhuma posição para resgatar.",
                "details": [],
                "error": None,
            }

        # size_threshold opcional na API: ignorar posições muito pequenas
        for i, pos in enumerate(positions):
            try:
                condition_id = getattr(pos, "condition_id", None)
                if not condition_id:
                    details.append(f"Posição {i+1}: sem condition_id")
                    failed += 1
                    continue

                size = getattr(pos, "size", None)
                if size is None:
                    size = getattr(pos, "tokens", 0)
                try:
                    size = float(size)
                except (TypeError, ValueError):
                    size = 0.0

                outcome_index = getattr(pos, "outcome_index", None) or getattr(pos, "outcomeIndex", 0)
                try:
                    outcome_index = int(outcome_index)
                except (TypeError, ValueError):
                    outcome_index = 0
                if outcome_index not in (0, 1):
                    outcome_index = 0

                neg_risk = getattr(pos, "negative_risk", False)
                if isinstance(neg_risk, str):
                    neg_risk = neg_risk.lower() in ("true", "1", "yes")

                amounts = [0.0, 0.0]
                if 0 <= outcome_index <= 1:
                    amounts[outcome_index] = size

                if amounts[0] == 0 and amounts[1] == 0:
                    details.append(f"Posição {i+1}: size inválido")
                    failed += 1
                    continue

                current_value = getattr(pos, "current_value", None) or getattr(pos, "value", None)
                if current_value is not None:
                    try:
                        total_value += float(current_value)
                    except (TypeError, ValueError):
                        pass

                result = web3.redeem_position(
                    condition_id=condition_id,
                    amounts=amounts,
                    neg_risk=neg_risk,
                )
                if result:
                    details.append(f"Resgatado: condition {condition_id[:16]}... → {result}")
                    redeemed += 1
                else:
                    details.append(f"Falha (sem tx): condition {condition_id[:16]}...")
                    failed += 1
            except Exception as e:
                logger.exception("Erro ao resgatar posição")
                details.append(f"Posição {i+1}: {e!s}")
                failed += 1

        msg = f"Resgatadas: {redeemed}, falhas: {failed}."
        if total_value > 0:
            msg += f" Valor total (aprox.): ${total_value:.2f}"
        return {
            "ok": True,
            "redeemed": redeemed,
            "failed": failed,
            "total_value": round(total_value, 2),
            "message": msg,
            "details": details,
            "error": None,
        }
    except Exception as e:
        logger.exception("run_claim error")
        return {
            "ok": False,
            "redeemed": redeemed,
            "failed": failed,
            "total_value": round(total_value, 2),
            "message": f"Erro: {e!s}",
            "details": details,
            "error": str(e),
        }
