# Verificação de segurança: pacote polymarket-apis

Este documento resume a análise de segurança do pacote **polymarket-apis** (PyPI), usado no projeto para **claim por API** (resgate de posições na Polymarket).

---

## 1. Identificação do pacote

- **Nome PyPI:** `polymarket-apis`
- **Versão analisada:** 0.4.6
- **Repositório:** https://github.com/qualiaenjoyer/polymarket-apis
- **Autor:** Razvan Gheorghe (razvan@gheorghe.me)
- **Licença:** não restritiva (projeto aberto no GitHub)

**Importante:** O pacote **polymarket-apis** é **diferente** do bot malicioso “polymarket-copy-trading-bot” (Trust412) que foi reportado em 2025 por roubo de chaves. Esse outro projeto lia `.env` e enviava credenciais para servidor externo. O polymarket-apis é uma biblioteca separada, só cliente de APIs e Web3.

---

## 2. Uso da chave privada

### Onde a chave é usada

- **`Signer`** (`utilities/signing/signer.py`): a chave é passada para `eth_account.Account.from_key(private_key)` e para `Account.unsafe_sign_hash(...)`. Tudo é **local**: assinatura de mensagens EIP-712 e hashes, sem envio da chave pela rede.
- **`PolymarketWeb3Client`** (`clients/web3_client.py`): a chave é usada por `SignAndSendRawMiddlewareBuilder.build(private_key)` do **web3.py**, que assina transações localmente e envia apenas **transação já assinada** (`send_raw_transaction`) para o RPC da Polygon.

### O que não acontece

- Nenhum trecho do código envia `private_key` em corpo de requisição HTTP, headers ou WebSocket.
- Nenhuma leitura de arquivo `.env` pelo polymarket-apis para exfiltrar dados.
- Nenhum `eval`/`exec` com conteúdo do usuário ou da chave.

No fluxo que **este projeto** usa (claim com `PolymarketWeb3Client` + gas), a chave **nunca sai do processo**: só assina localmente e envia tx bruta para o RPC.

---

## 3. Destinos de rede

Todos os destinos encontrados no código são serviços oficiais ou documentados do ecossistema Polymarket/Polygon:

| Destino | Uso |
|--------|-----|
| `https://clob.polymarket.com` | API CLOB (ordens, book, auth L1/L2). |
| `https://data-api.polymarket.com` | API de dados (posições, trades, activity). |
| `https://gamma-api.polymarket.com` | API Gamma (eventos, mercados). |
| `https://relayer-v2.polymarket.com` | Relayer oficial Polymarket (transações gasless). |
| `https://polygon.drpc.org` | RPC Polygon (default; envio de transações assinadas). |
| `https://api.goldsky.com/.../subgraphs/...` | Subgraphs Goldsky usados pela Polymarket. |
| `https://polymarket.com/api/...` | APIs públicas do site (ex.: grok, rewards). |
| `https://builder-signing-server.vercel.app/sign` | Usado **apenas** pelo cliente **gasless** quando não há `builder_creds`: envia **só** o corpo da transação de relay para obter headers de builder; **não** envia chave privada. |

**No nosso projeto:** usamos apenas `PolymarketWeb3Client` (com gas) e `PolymarketDataClient`. Não usamos o cliente gasless, então o builder-signing-server não é chamado pelo nosso fluxo de claim.

Nenhum domínio desconhecido ou suspeito foi encontrado para envio de credenciais ou dados sensíveis.

---

## 4. Endereços de contratos

Em `utilities/config.py` os endereços são os **contratos oficiais** Polymarket na Polygon (chain_id 137), documentados em docs.polymarket.com e PolygonScan, por exemplo:

- **Exchange:** `0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E` (CTF Exchange)
- **Collateral (USDC):** `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`
- **Conditional Tokens:** `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045`
- **Neg Risk Exchange / Adapter:** endereços conhecidos do ecossistema Polymarket

Não há endereço de carteira “receptora” hardcoded que pudesse desviar fundos.

---

## 5. Dependências (pyproject.toml)

Dependências diretas do polymarket-apis:

- `python-dateutil`, `pydantic`, `httpx`, `web3`, `lomond`, `wsaccel`, `gql`
- `poly-eip712-structs`, `py-order-utils`

Nenhuma dependência com histórico conhecido de malware ou exfiltração de chaves foi identificada. São libs comuns para Web3, HTTP e assinatura EIP-712.

---

## 6. Resumo e recomendações

### Conclusão

- **Chave privada:** usada apenas para assinatura local (Signer + web3); não é enviada por rede no fluxo de claim que usamos.
- **Rede:** apenas destinos Polymarket/Polygon/Goldsky oficiais ou documentados; nenhum servidor suspeito.
- **Contratos:** endereços oficiais Polymarket; nenhum endereço de “recebedor” fixo no código.
- **Dependências:** sem indício de pacote malicioso nas dependências diretas.

Com base nessa análise, **não há indício de código malicioso no polymarket-apis** voltado a roubo de carteira ou de chave privada no uso que fazemos (claim por API com `PolymarketWeb3Client` e `PolymarketDataClient`).

### Boas práticas recomendadas

1. Manter **polymarket-apis** atualizado e acompanhar releases/security no repositório.
2. **Não** commitar `.env` ou chaves no repositório; usar variáveis de ambiente ou secretos no servidor.
3. Em produção, considerar **fixar a versão** no `requirements.txt` (ex.: `polymarket-apis==0.4.6`) e atualizar com testes após cada upgrade.
4. Se no futuro usar cliente **gasless** (`PolymarketGaslessWeb3Client`), estar ciente de que ele chama `builder-signing-server.vercel.app` para obter headers de builder; mesmo assim, a chave privada não é enviada nessa chamada.

---

*Verificação feita com base no código do pacote instalado (venv) e no repositório público qualiaenjoyer/polymarket-apis.*
