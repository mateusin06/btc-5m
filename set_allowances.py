#!/usr/bin/env python3
"""
Configura allowances (permissões) para MetaMask/EOA operar via API na Polymarket.

Usuários de MetaMask precisam rodar este script UMA VEZ antes de usar o bot via API.
O depósito no site da Polymarket não configura os contratos que a API usa.

Requer: POLY_PRIVATE_KEY no .env e um pouco de MATIC/POL na carteira para gas.
"""

import os
from dotenv import load_dotenv

load_dotenv()

RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon.drpc.org")
CHAIN_ID = 137

# Contratos Polymarket (Polygon)
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC.e
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Tokens

# Contratos que precisam de approval
EXCHANGES = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",  # CTF Exchange
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",  # Neg Risk CTF Exchange
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # Neg Risk Adapter
]

ERC20_APPROVE_ABI = [{"constant": False, "inputs": [{"name": "_spender", "type": "address"}, {"name": "_value", "type": "uint256"}], "name": "approve", "outputs": [{"name": "", "type": "bool"}], "payable": False, "stateMutability": "nonpayable", "type": "function"}]
ERC1155_SET_APPROVAL_ABI = [{"inputs": [{"internalType": "address", "name": "operator", "type": "address"}, {"internalType": "bool", "name": "approved", "type": "bool"}], "name": "setApprovalForAll", "outputs": [], "stateMutability": "nonpayable", "type": "function"}]


def run_set_allowances(
    private_key: str,
    rpc_url: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    api_passphrase: str | None = None,
) -> tuple[bool, str, list[str]]:
    """
    Configura allowances para a carteira. Retorna (ok, message, details).
    Se api_key/secret/passphrase forem fornecidos, chama update_balance_allowance
    para a API CLOB reconhecer o saldo (necessário para ordens funcionarem).
    """
    key = (private_key or "").strip()
    if not key or key == "0x...":
        return False, "Chave privada inválida.", []

    try:
        from web3 import Web3
        from web3.constants import MAX_INT
        from web3.middleware import ExtraDataToPOAMiddleware
    except ImportError:
        return False, "Instale web3: pip install web3", []

    url = rpc_url or RPC_URL
    w3 = Web3(Web3.HTTPProvider(url))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

    if not w3.is_connected():
        return False, "Não foi possível conectar à Polygon RPC.", []

    account = w3.eth.account.from_key(key)
    pub_key = account.address

    balance = w3.eth.get_balance(pub_key)
    balance_eth = float(w3.from_wei(balance, "ether"))
    details: list[str] = [f"Endereço: {pub_key}", f"Saldo MATIC/POL: {balance_eth:.6f}"]
    if balance_eth < 0.001:
        return False, "Saldo de MATIC/POL insuficiente para gas. Envie um pouco de MATIC para a carteira na Polygon.", details

    usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_APPROVE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_SET_APPROVAL_ABI)

    for exchange in EXCHANGES:
        exchange = Web3.to_checksum_address(exchange)
        try:
            # 'pending' inclui nossas próprias tx pendentes, evitando "nonce too low"
            nonce = w3.eth.get_transaction_count(pub_key, "pending")
            tx_usdc = usdc.functions.approve(exchange, int(MAX_INT, 0)).build_transaction({
                "chainId": CHAIN_ID, "from": pub_key, "nonce": nonce
            })
            signed_usdc = w3.eth.account.sign_transaction(tx_usdc, private_key=key)
            tx_hash = w3.eth.send_raw_transaction(signed_usdc.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            details.append(f"USDC approve {exchange[:10]}...: {'ok' if receipt['status'] == 1 else 'falhou'}")

            nonce = w3.eth.get_transaction_count(pub_key, "pending")
            tx_ctf = ctf.functions.setApprovalForAll(exchange, True).build_transaction({
                "chainId": CHAIN_ID, "from": pub_key, "nonce": nonce
            })
            signed_ctf = w3.eth.account.sign_transaction(tx_ctf, private_key=key)
            tx_hash = w3.eth.send_raw_transaction(signed_ctf.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            details.append(f"CTF setApprovalForAll {exchange[:10]}...: {'ok' if receipt['status'] == 1 else 'falhou'}")
        except Exception as e:
            return False, f"Erro ao configurar allowances: {e!s}", details

    # Atualizar balance/allowance na API CLOB (necessário para a API reconhecer o saldo)
    if api_key and api_secret and api_passphrase:
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

            client = ClobClient(
                "https://clob.polymarket.com",
                chain_id=137,
                key=key,
                signature_type=0,
                funder=None,
            )
            client.set_api_creds(ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase))
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
            client.update_balance_allowance(params=params)
            details.append("update_balance_allowance (CLOB API): ok")
        except Exception as e:
            details.append(f"update_balance_allowance: {e!s} (ordens podem falhar)")
            # Não falhar - os approves on-chain já foram feitos

    return True, "Allowances configurados. Agora você pode operar via API.", details


def main():
    key = (os.getenv("POLY_PRIVATE_KEY") or "").strip()
    if not key or key == "0x...":
        print("ERRO: Defina POLY_PRIVATE_KEY no .env")
        return 1

    ok, msg, details = run_set_allowances(key)
    for d in details:
        print(d)
    print(f"\n{'✓' if ok else 'ERRO:'} {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    exit(main() or 0)
