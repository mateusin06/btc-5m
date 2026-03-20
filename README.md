# Polymarket BTC 5-Min Up/Down Trading Bot

Bot de trading para os mercados binários "BTC Up or Down" de 5 minutos na Polymarket. Usa análise técnica em dados da Binance para prever a direção e executa ordens automaticamente (ou arbitragem quando há lucro garantido).

## O que o bot faz

A cada 5 minutos, a Polymarket abre um mercado: "O BTC estará mais alto ou mais baixo que o preço de abertura quando a janela fechar?" Você compra tokens "Up" ou "Down" (ex: $0.50–$0.95). Se acertar, cada token paga $1.00. Se errar, perde a aposta.

O bot usa análise técnica em tempo real (Binance) para prever o resultado. Nos modos **safe**, **aggressive** e **degen** ele entra na operação quando faltam **2 minutos ou menos** para o fechamento (os 3 primeiros minutos da janela ficam em espera para ter mais informação antes de decidir). No modo **arbitragem** ele monitora e pode operar **desde o início da janela** para captar oportunidades de lucro garantido. Nenhum token é comprado acima de **90c** (configurável via `MAX_TOKEN_PRICE`).

## Arquivos

| Arquivo | Função |
|---------|--------|
| `bot.py` | Engine principal — timing, ordens, modos, bankroll |
| `strategy.py` | Análise técnica — sinal composto de 7 indicadores |
| `compare_runs.py` | Backtesting — testa várias configs, gera Excel |
| `api.py` | APIs Binance e Polymarket Gamma (preços, resolução Chainlink/Price to Beat) |
| `setup_creds.py` | Setup único — deriva credenciais da chave privada |
| `web.py` | Dashboard — API e frontend para config, start/stop e estatísticas |

## Dashboard (frontend)

O projeto inclui uma **dashboard web** para configurar o bot, derivar a API da chave privada, iniciar/parar em modo **safe**, **agressivo** ou **dry run**, e ver **estatísticas por período** (24h, 7d, 30d).

### Subir a dashboard

```bash
venv\Scripts\activate
pip install -r requirements.txt   # inclui fastapi, uvicorn
python web.py
```

Acesse **http://localhost:8000** no navegador.

### O que a dashboard faz

- **Config & API:** colar a chave privada e o Funder Address, clicar em **Gerar API** (usa a mesma lógica do `setup_creds.py`) e salvar a config no Supabase. Ao iniciar o bot, a dashboard passa todas as variáveis por ambiente (não usa arquivo .env). Também é possível ajustar bankroll, aposta mínima, aposta Safe (USD), % Agressivo, % Arbitragem, preço máximo do token e lucro mínimo da arb.
- **Iniciar bot:** escolher modo (Safe, Agressivo, Dry run, Arbitragem), informar valor ou % conforme o modo, e clicar em **Iniciar** ou **Parar**. O bot roda em subprocesso e a saída vai para `resultados.txt`.
- **Estatísticas:** ver totais de trades, vitórias, derrotas, arbs, PnL e win rate para os últimos 24h, 7d ou 30d (dados em `data/trades.jsonl`, preenchido pelo bot a cada trade).
- **Log:** últimas linhas de `resultados.txt`.

O modo **agressivo** usa o % salvo na config do usuário (`AGGRESSIVE_BET_PCT`, ex: 25). Safe e arbitragem podem ser definidos ao iniciar pela dashboard ou salvos na config.

### Login e config por usuário (Supabase)

A dashboard usa **Supabase** para login (e-mail + senha) e para guardar a config de cada usuário (chave privada, API, funder address, parâmetros do bot). Assim cada pessoa tem sua conta e não precisa digitar as credenciais a cada acesso.

1. **Criar tabela no Supabase:** no painel do seu projeto Supabase, abra **SQL Editor**, cole e execute o conteúdo do arquivo `supabase/migrations/001_user_config.sql`. Isso cria a tabela `user_config` e as políticas RLS (cada usuário só acessa os próprios dados).

2. **Auth por e-mail:** em **Authentication > Providers**, certifique-se de que **Email** está habilitado. Se quiser que novos usuários entrem sem confirmar e-mail, em **Authentication > Settings** desative "Confirm email".

3. **URL e chave:** o `web.py` usa a URL e a chave anon do Supabase. Para outro projeto, defina as variáveis de ambiente `SUPABASE_URL` e `SUPABASE_ANON_KEY` (no sistema ou no processo que inicia o servidor).

4. **Uso:** ao abrir a dashboard, aparece a tela de **Entrar** / **Criar conta**. Após o login, a config é carregada do Supabase; ao salvar, ela é gravada na sua conta. Trades e log ficam separados por usuário (`data/trades_<user_id>.jsonl` e `resultados_<user_id>.txt`).

5. **Trial e assinatura (acesso pago):** execute a migration `supabase/migrations/003_trial_subscription.sql` para adicionar `trial_ends_at` e `subscription_ends_at` em `user_config`. Novos usuários ganham 2 dias grátis; após isso, o bot só inicia se houver assinatura ativa. Para liberar 30 dias manualmente, use a aba **Admin** (visível só para o e-mail configurado como admin). A carteira para recebimento de 100 USDC e a URL/chave do Supabase estão definidas no código (`web.py`).

## Instalação

### 1. Python 3.10+

```bash
python -m venv venv
venv\Scripts\activate   # Windows
# ou: source venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
```

### 2. Configurar credenciais

A config é passada pela **dashboard** ao iniciar o bot (Supabase); não é obrigatório usar arquivo `.env`. Para rodar o bot ou scripts manualmente (ex.: `setup_creds.py`, `check_balance.py`), defina as variáveis de ambiente (por exemplo com `export` no terminal). Opcionalmente você pode criar um arquivo `.env` na raiz do projeto (veja `.env.example` como referência das variáveis).

Variáveis principais (referência em `env.example`):

- **POLY_PRIVATE_KEY** — Chave privada da carteira (com `0x`)
- **POLY_FUNDER_ADDRESS** — Endereço que detém os fundos (proxy/carteira)
- **POLY_SIGNATURE_TYPE** — `0` = MetaMask/EOA, `1` = Magic/Email, `2` = Proxy
- **STARTING_BANKROLL** — Bankroll inicial em USDC
- **MIN_BET** — Aposta mínima (Polymarket exige mínimo; ex: 2.5 ou 5.0)
- **MAX_TOKEN_PRICE** — Preço máximo por token em dólares (ex: 0.90 = 90c)
- **ARB_MIN_PROFIT_PCT** — (Opcional) Lucro mínimo para arbitragem (ex: 0.04 = 4%)

Para derivar as credenciais da API (uma vez):

```bash
python setup_creds.py
```

Use as linhas geradas (`POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`) no ambiente ou na config da dashboard.

### 3. Carteira e saldo

- Conta na Polymarket com USDC na rede Polygon
- Saldo suficiente para apostas (mínimo conforme `MIN_BET`)

## Uso

### Modo safe (valor fixo em USD)

O bot pergunta no terminal o valor fixo de entrada. Para rodar com saída redirecionada (ex: `> resultados.txt 2>&1`), use `--safe-bet`:

```bash
python bot.py --mode safe
# ou com valor fixo direto:
python bot.py --mode safe --safe-bet 5.0
python bot.py --dry-run --mode safe --safe-bet 2.5 > resultados.txt 2>&1
```

### Modo aggressive (25% da banca)

Aposta 25% do bankroll; se o bankroll estiver acima do inicial, aposta apenas o lucro (protege o principal).

```bash
python bot.py --mode aggressive
python bot.py --dry-run --mode aggressive
```

### Modo arbitragem (% da banca + arb pura)

O bot pergunta a % da banca por entrada (ou use `--arbitragem-pct`). Monitora desde o **início da janela**. Primeiro tenta **arb pura** (comprar Up e Down quando a soma dos preços dá lucro garantido); se não houver, faz aposta direcional e tenta hedge no outro lado. Se não encontrar oportunidade de hedge, a aposta segue normalmente (estratégia simples).

```bash
python bot.py --mode arbitragem
# ou com % direto (ex: 25%):
python bot.py --mode arbitragem --arbitragem-pct 25
python bot.py --dry-run --mode arbitragem --arbitragem-pct 25 > resultados.txt 2>&1
```

### Dry run (simula sem ordens reais)

No dry run o bot usa preço real da Polymarket (CLOB ou Gamma) quando disponível, espera até **2 minutos** pela resolução via **Chainlink** (Price to Beat da próxima janela) para bater com o site e mostra WIN/LOSS com slug da operação.

```bash
python bot.py --dry-run --mode safe --safe-bet 5.0
python bot.py --dry-run --mode arbitragem --arbitragem-pct 25
```

### Modos resumido

| Modo | Aposta | Quando entra | Confiança mín. |
|------|--------|--------------|----------------|
| **safe** | Valor fixo em USD (perguntado ou `--safe-bet`) | Últimos 2 min | 72% |
| **aggressive** | 25% da banca (ou só lucros se bankroll > inicial) | Últimos 2 min | 58% |
| **only_hedge_plus** | Valor fixo em USD | Últimos 2 min | 72% + só entra com EV+ (P(win) > preço + margem) |
| **degen** | 100% da banca | Últimos 2 min | 0% |
| **arbitragem** | % da banca (perguntado ou `--arbitragem-pct`) | **Desde o início da janela** | 30% — prioriza arb pura; senão aposta direcional + hedge; sem hedge = aposta normal |

### Outros exemplos

```bash
# Um único ciclo
python bot.py --dry-run --once

# Limitar trades no dry run
python bot.py --dry-run --max-trades 20 --mode safe --safe-bet 5.0

# Backtesting
python compare_runs.py --hours 72 --output results.xlsx
```

## Estratégia

O sinal direcional é composto por 7 indicadores ponderados (ajustados para ser mais assertivos de dia):

1. **Window Delta** (peso 1–7) — Principal: BTC acima ou abaixo do preço de abertura da janela (≥0,2% para peso 1)
2. **Micro Momentum** (1,5) — Direção dos últimos 2 candles 1min
3. **Aceleração** (1,2) — Momentum crescendo ou diminuindo
4. **EMA 9/21** (1,2) — Cruzamento de médias
5. **RSI 14** (±2) — Só extremos fortes (RSI ≤20 ou ≥80)
6. **Volume Surge** (1,2) — Volume recente ≥2× o anterior
7. **Tick Trend** (1,2) — Tendência em ticks (movimento ≥0,008% e consistência ≥65% ou ≤35%)

Score positivo → Up, negativo → Down. Confiança = `min(|score|/MAX_SCORE, 1)`. Entrada por **Spike** (salto ≥2,5 + confiança ≥40%), **Confiança** (por modo) ou **T-5s** (confiança ≥40%).

**Arbitragem:** o bot primeiro verifica se comprar Up e Down ao mesmo tempo dá lucro garantido (soma dos preços ≤ 1 − `ARB_MIN_PROFIT_PCT`). Se sim, executa as duas pernas. Se não, aposta no lado do sinal de TA e tenta comprar o lado oposto a preço que dê lucro; se não achar, fica só com a aposta direcional.

## Avisos

- **Risco**: Trading envolve perda de capital. Use apenas o que pode perder.
- **Dry run primeiro**: Teste com `--dry-run` antes de operar com dinheiro real.
- **Allowances**: Usuários MetaMask/EOA precisam aprovar tokens (USDC e Conditional Tokens) nos contratos da Polymarket. Magic/Email costuma ter isso automático.
- **Safe/arbitragem com redirecionamento**: Se rodar com `> arquivo.txt 2>&1`, use `--safe-bet` e `--arbitragem-pct` para evitar prompt no terminal.

## Licença

Uso por sua conta e risco.
