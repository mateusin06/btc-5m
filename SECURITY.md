# Relatório de Segurança — Polymarket Bot Dashboard

## Resumo

Foi feita uma análise de segurança no backend (FastAPI), frontend, bot e integração com Supabase. **Correções aplicadas** estão descritas abaixo; **recomendações** devem ser seguidas em produção.

---

## Correções já aplicadas

### 1. **Path traversal na rota estática** (web.py)
- **Risco:** A rota `GET /{path:path}` servia arquivos com base em `path` sem validar se o caminho resolvido permanecia dentro de `frontend/`. Um atacante poderia pedir `../../../etc/passwd` e tentar ler arquivos do servidor.
- **Correção:** O caminho é resolvido com `.resolve()` e verificado com `relative_to(FRONTEND_DIR)`. Se sair do diretório do frontend, retorna 404.

### 2. **Parâmetro `tail` em /api/logs**
- **Risco:** `tail` sem limite permitia valores altos (ex.: 999999), podendo causar alto uso de memória e CPU.
- **Correção:** `tail` é limitado entre 1 e 500 (`MAX_LOG_TAIL = 500`).

### 3. **Validação de `mode` e `bot_mode`**
- **Risco:** Valores arbitrários em `BotStartRequest.mode` e `ConfigUpdate.bot_mode` podiam ser repassados ao subprocesso ou ao Supabase.
- **Correção:** Uso de `Literal["safe", "aggressive", "dry_run", "arbitragem"]` em `BotStartRequest` e `Literal["safe", "aggressive", "degen", "arbitragem"]` em `ConfigUpdate.bot_mode`. Pydantic rejeita outros valores.

### 4. **Parâmetro `period` em /api/stats**
- **Risco:** Qualquer string era aceita (a lógica interna já tratava apenas 24h/7d/30d, mas a API não restringia).
- **Correção:** `period` tipado como `Literal["24h", "7d", "30d"]`.

### 5. **Vazamento de detalhes em respostas de erro**
- **Risco:** `HTTPException(detail=str(e))` em `_config_to_supabase` e em `derive_creds` podia expor mensagens internas ou caminhos de arquivo.
- **Correção:** Mensagens genéricas em produção para erros de config e para exceções não controladas em `derive_creds`; apenas `ValueError` em `derive_creds` mantém mensagem específica (ex.: “Chave privada inválida”).

### 6. **`signature_type` em derive-creds**
- **Risco:** Valores fora de 0/1/2 poderiam ser enviados ao cliente Polymarket.
- **Correção:** Validação explícita no endpoint; retorna 400 se não for 0, 1 ou 2.

---

## Pontos já seguros (sem alteração)

- **Autenticação:** JWT do Supabase validado via `GET /auth/v1/user`; rotas sensíveis usam `Depends(get_current_user)`.
- **RLS no Supabase:** Políticas garantem que cada usuário acesse apenas a própria linha em `user_config`.
- **Subprocesso do bot:** Comando montado com lista (`[sys.executable, "bot.py", "--mode", mode, ...]`), sem `shell=True`; não há injeção de comando.
- **User ID em arquivos:** `_safe_user_id()` restringe a `[a-zA-Z0-9\-]` e tamanho 64; nomes de arquivo `resultados_{safe_id}.txt` e `trades_{safe_id}.jsonl` não permitem path traversal.
- **Bot (bot.py):** `BOT_USER_ID` sanitizado com regex antes de usar em caminho de arquivo; escrita em arquivos sob o projeto.
- **Supabase / API externa:** Uso de parâmetros/JSON nas requisições; não há concatenação de SQL ou de path com input do usuário.
- **Frontend:** Exibição de dados da API sem `innerHTML` com conteúdo do usuário; chave anon do Supabase em `/api/public-config` é esperada para uso no cliente (SPA).

---

## Recomendações para produção

### 1. **Secrets e variáveis de ambiente**
- **Não** commitar `SUPABASE_ANON_KEY` ou `SUPABASE_URL` com valores reais no código. Usar apenas variáveis de ambiente (ex.: na Vercel) e remover defaults sensíveis em `web.py` se o repositório for público.
- Manter `.env` no `.gitignore` se usar esse arquivo; nunca commitar chaves privadas ou API keys. A config em produção pode ser só por variáveis de ambiente (dashboard passa as credenciais ao iniciar o bot).

### 2. **CORS**
- Hoje: `allow_origins=["*"]` com `allow_credentials=True`. Em produção, definir origens explícitas, por exemplo:
  - `allow_origins=["https://seu-dominio.vercel.app", "https://seu-dominio.com"]`.

### 3. **Rate limiting**
- Endpoints como `POST /api/derive-creds` e `POST /api/config` podem ser alvo de abuso (ex.: muitas requisições por minuto). Recomenda-se rate limiting por IP ou por usuário (ex.: `slowapi` ou proxy na Vercel).

### 4. **Chaves privadas no Supabase**
- A tabela `user_config` guarda `private_key` em texto. O Supabase criptografa em repouso; para maior segurança, considerar criptografia adicional no app (ex.: chave derivada da senha do usuário) ou uso de um vault (ex.: Supabase Vault). Isso exige mudança de fluxo e armazenamento.

### 5. **HTTPS**
- Em produção, servir o site e a API somente via HTTPS. A Vercel já faz isso para o domínio dela.

### 6. **Logs e monitoramento**
- Evitar logar tokens, chaves privadas ou trechos de config. Em caso de erro, logar apenas tipo de erro e IDs (ex.: `user_id`) quando necessário para suporte.

### 7. **Dependências**
- Rodar periodicamente `pip audit` (ou equivalente) e atualizar dependências com vulnerabilidades conhecidas.

---

## Contato

Em caso de descoberta de vulnerabilidade, reporte de forma responsável (por exemplo por um canal privado com o mantenedor do projeto).
