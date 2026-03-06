#!/usr/bin/env python3
"""
Setup de credenciais Polymarket - Deriva API keys da chave privada.

Execute uma vez para obter POLY_API_KEY, POLY_API_SECRET e POLY_API_PASSPHRASE.
Cole os valores no ambiente (export no terminal ou config do servidor) ou no arquivo .env se usar.
"""

import os
import sys
from dotenv import load_dotenv

# Carrega .env apenas se o arquivo existir (config pode ser só por variáveis de ambiente)
if os.path.exists(os.path.join(os.path.dirname(__file__) or ".", ".env")):
    load_dotenv()

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon


def main() -> None:
    private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    if not private_key or private_key == "0x...":
        print("ERRO: Defina POLY_PRIVATE_KEY (variável de ambiente) com sua chave privada.")
        print("Exemplo: POLY_PRIVATE_KEY=0x1234...")
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
    except ImportError:
        print("ERRO: Instale as dependências: pip install -r requirements.txt")
        sys.exit(1)

    funder = os.getenv("POLY_FUNDER_ADDRESS", "").strip()
    sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1"))

    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        signature_type=sig_type,
        funder=funder if funder else None,
    )

    print("Derivando credenciais da chave privada...")
    creds = client.create_or_derive_api_creds()

    if not creds:
        print("ERRO: Falha ao derivar credenciais.")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("Defina estas variáveis de ambiente (ou adicione ao seu ambiente antes de rodar o bot):")
    print("=" * 60)
    print(f"POLY_API_KEY={creds.api_key}")
    print(f"POLY_API_SECRET={creds.api_secret}")
    print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
    print("=" * 60)
    print("\nPronto! O bot pode usar credenciais L2 para trading.")


if __name__ == "__main__":
    main()
