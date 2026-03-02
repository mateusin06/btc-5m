#!/usr/bin/env python3
"""
Script de diagnóstico: verifica saldo e allowance na API Polymarket.
Use para debugar "not enough balance / allowance".

Uso: defina as variáveis no .env e rode:
  python check_balance.py

Ou passe as env vars:
  POLY_PRIVATE_KEY=0x... POLY_FUNDER_ADDRESS=0x... POLY_SIGNATURE_TYPE=2 python check_balance.py
"""

import os
import sys
from pathlib import Path

# Carrega .env do projeto
Path(__file__).resolve().parent
from dotenv import load_dotenv
load_dotenv()

def main():
    key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "0"))
    api_key = os.getenv("POLY_API_KEY", "").strip()
    api_secret = os.getenv("POLY_API_SECRET", "").strip()
    api_pass = os.getenv("POLY_API_PASSPHRASE", "").strip()

    print("=== Diagnóstico Polymarket ===\n")
    print(f"signature_type: {sig_type} ({'EOA' if sig_type == 0 else 'Magic' if sig_type == 1 else 'Proxy/Safe'})")
    print(f"funder: {funder[:10]}...{funder[-4:] if len(funder) >= 14 else ' (vazio)'}")
    print(f"api_key definida: {bool(api_key)}")
    print()

    if not key or not api_key or not api_secret or not api_pass:
        print("ERRO: Defina POLY_PRIVATE_KEY, POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE no .env")
        return 1

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
    except ImportError:
        print("ERRO: pip install py-clob-client")
        return 1

    client = ClobClient(
        "https://clob.polymarket.com",
        chain_id=137,
        key=key,
        creds=ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_pass),
        signature_type=sig_type,
        funder=funder or None,
    )

    addr = client.get_address()
    print(f"Endereço que a API usa: {addr}")
    print()

    # 1. update_balance_allowance
    print("1. Chamando update_balance_allowance...")
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        client.update_balance_allowance(params=params)
        print("   OK")
    except Exception as e:
        print(f"   FALHOU: {e}")
        return 1

    # 2. get_balance_allowance
    print("2. Chamando get_balance_allowance...")
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=sig_type)
        resp = client.get_balance_allowance(params)
        if resp:
            bal = resp.get("balance")
            allowance = resp.get("allowance")
            print(f"   balance: {bal}")
            print(f"   allowance: {allowance}")
            if bal is not None:
                b = float(bal)
                if b < 1:
                    print(f"\n   AVISO: Saldo baixo (${b:.2f}). Deposite USDC na Polymarket.")
                else:
                    print(f"\n   Saldo OK: ${b:.2f}")
            else:
                print("\n   AVISO: API não retornou balance. Verifique funder e signature_type.")
        else:
            print("   Resposta vazia")
    except Exception as e:
        print(f"   FALHOU: {e}")
        return 1

    print("\n=== Fim do diagnóstico ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
