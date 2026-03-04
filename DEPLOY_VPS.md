# Deploy 24/7 na VPS (Hostinger ou outra)

Guia passo a passo para rodar o **dashboard e o bot Polymarket** em um VPS Ubuntu (ex.: Hostinger KVM). O servidor fica ligado 24/7; o site e a API respondem o tempo todo. **Vários usuários podem usar ao mesmo tempo**, cada um com seu próprio bot.

---

## Visão geral dos passos

| # | O que faz |
|---|-----------|
| 1 | Conectar na VPS por SSH |
| 2 | Instalar Python, pip, venv, git, nginx (e opcionalmente Python 3.12) |
| 3 | Clonar o repositório do projeto |
| 4 | Criar ambiente virtual (venv) e instalar dependências |
| 5 | Criar o arquivo `.env` com as chaves do Supabase |
| 6 | Testar o dashboard na mão (uvicorn) |
| 7 | Configurar o serviço systemd para rodar 24/7 |
| 8 | Configurar o Nginx (site + proxy da API) e opcionalmente HTTPS |
| 9 | (Opcional) Configurar firewall |

---

## O que você vai ter no final

- Um site (ex.: `https://polybtc5m.duckdns.org`) com login e dashboard.
- Cada usuário faz login, salva as credenciais da Polymarket na aba Config e clica em **Iniciar** para rodar o bot.
- **Vários usuários podem ter o bot rodando ao mesmo tempo** — um processo por pessoa; stats e logs são separados por usuário.

---

## Pré-requisitos

1. **VPS Ubuntu** (Hostinger KVM 1 ou 2 é suficiente para começar; para muitos usuários simultâneos, considere KVM 2 ou superior).
2. **Domínio** apontando para o IP da VPS (opcional mas recomendado para HTTPS).
3. **Conta no Supabase** com o projeto criado e a tabela `user_config` aplicada:
   - No [Supabase](https://supabase.com), crie um projeto (ou use um existente).
   - Vá em **SQL Editor** → **New query**.
   - Copie e execute o conteúdo do arquivo `supabase/migrations/001_user_config.sql` deste repositório.
   - Depois execute também `supabase/migrations/002_only_hedge_bet.sql` (se existir).
   - Assim a tabela e as permissões (RLS) ficam criadas para o dashboard.
4. **Acesso SSH** ao VPS (usuário root ou com sudo).

---

## Passo 1: Conectar na VPS

No seu PC (PowerShell ou terminal):

```bash
ssh root@SEU_IP_VPS
```

Substitua `SEU_IP_VPS` pelo IP que a Hostinger forneceu. Se usar chave SSH, o comando é o mesmo; a senha ou a chave será pedida.

---

## Passo 2: Atualizar o sistema e instalar Python e dependências

Instale Python, pip, venv e os demais pacotes necessários:

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx
```

- **python3 / python3-pip / python3-venv** — para rodar o bot, o backend e o claim por API.
- **git** — para clonar o repositório.
- **nginx** — para servir o site e fazer proxy para a API.

**Python 3.12 (recomendado para claim por API):** O recurso de **Resgatar agora** e **Auto-claim** usa o pacote `polymarket-apis`, que exige **Python ≥ 3.12**. No Ubuntu 22.04/24.04 o `python3` padrão pode ser 3.10 ou 3.11. Se quiser usar o claim por API na VPS, instale Python 3.12 e use-o no venv:

```bash
apt install -y software-properties-common
add-apt-repository -y ppa:deadsnakes/ppa
apt update
apt install -y python3.12 python3.12-venv python3.12-dev
```

Depois, no Passo 4, crie o venv com `python3.12 -m venv venv` em vez de `python3 -m venv venv`.

(Opcional, se for usar scripts que usam Playwright: `apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2`.)

---

## Passo 3: Clonar o repositório do bot

Troque a URL pelo seu repositório (GitHub/GitLab):

```bash
cd /root
git clone https://github.com/mateusin06/btc-5m
cd btc-5m
```

Se você não usa Git ainda, pode enviar os arquivos por SFTP/SCP para `/root/BOT_Polymarket`.

---

## Passo 4: Ambiente virtual e dependências Python

Se você instalou **Python 3.12** (para claim por API), use:

```bash
python3.12 -m venv venv
```

Caso contrário (só Python padrão do sistema):

```bash
python3 -m venv venv
```

Em seguida:

```bash
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Se o projeto usar Playwright (ex.: `auto_claim.py`):

```bash
playwright install chromium
playwright install-deps
```

Mantenha o terminal com `source venv/bin/activate` ativo nos próximos passos (ou ative de novo ao abrir outro terminal).

---

## Passo 5: Arquivo `.env` no servidor

O `.env` na VPS é usado pelo **servidor** (dashboard). As credenciais da Polymarket de cada usuário ficam no **Supabase** (cada um preenche na aba Config após o login).

**Importante:** o arquivo `.env` deve ficar **dentro da pasta do projeto** (`/root/BOT_Polymarket`), pois o uvicorn carrega as variáveis a partir dali. Você já está nessa pasta após o Passo 3.

Crie o arquivo:

```bash
nano .env
```

Coloque **apenas** o que o dashboard precisa (Supabase). Exemplo:

```env
SUPABASE_URL=https://seu-projeto.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

- **SUPABASE_URL** — em Supabase: **Settings** (ícone de engrenagem) → **API** → "Project URL".
- **SUPABASE_ANON_KEY** — chave “anon” pública do mesmo projeto.

Salve: `Ctrl+O`, Enter, depois `Ctrl+X`.

Não é obrigatório colocar `POLY_*` no `.env` do servidor; cada usuário configura as próprias chaves no site.

---

## Passo 6: Testar na mão

Antes de configurar serviço e Nginx, confira se o app sobe. Certifique-se de estar na pasta do projeto e com o venv ativo:

```bash
cd /root/BOT_Polymarket
source venv/bin/activate
uvicorn web:app --host 0.0.0.0 --port 8000
```

Acesse no navegador: `http://62.72.23.75:8000`. Deve abrir o dashboard. Pare o servidor com `Ctrl+C`.

---

## Passo 7: Serviço systemd (rodar 24/7)

Assim o dashboard (e a lógica de start/stop dos bots) continua rodando mesmo após você desconectar.

Crie o arquivo do serviço:

```bash
nano /etc/systemd/system/polymarket-bot.service
```

Cole (ajuste o caminho se não for `/root/BOT_Polymarket`):

```ini
[Unit]
Description=Polymarket Bot Dashboard
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/btc-5m
Environment=PATH=/root/btc-5m/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/root/btc-5m/venv/bin/uvicorn web:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

- **127.0.0.1** — a API fica só local; o Nginx faz proxy e fica exposto na porta 80/443.

Ative e inicie:

```bash
systemctl daemon-reload
systemctl enable polymarket-bot
systemctl start polymarket-bot
systemctl status polymarket-bot
```

Se aparecer “active (running)”, está ok. Para ver logs:

```bash
journalctl -u polymarket-bot -f
```

---

## Passo 8: Nginx (site e HTTPS)

O Nginx recebe o tráfego na porta 80 (e 443 com certificado) e repassa para o uvicorn na porta 8000.

### 8.1 Site em HTTP (só IP ou domínio sem HTTPS)

Crie o arquivo de site:

```bash
nano /etc/nginx/sites-available/polymarket
```

Se for acessar por **IP**:

```nginx
server {
    listen 80 default_server;
    server_name polybtc5m.duckdns.org;
    root /root/BOT_Polymarket/frontend;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Se tiver **domínio** (ex.: DuckDNS `polybtc5m.duckdns.org`), troque `server_name _;` por:

```nginx
server_name polybtc5m.duckdns.org;
```

Ative o site e recarregue o Nginx:

```bash
ln -sf /etc/nginx/sites-available/polymarket /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

Acesse `http://SEU_IP` ou `http://polybtc5m.duckdns.org`. Deve carregar o dashboard.

### 8.2 HTTPS com Certbot (recomendado)

Instale o Certbot e gere o certificado (use o domínio que aponta para esta VPS, ex.: `polybtc5m.duckdns.org`):

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d polybtc5m.duckdns.org
```

Siga as perguntas (e-mail, aceitar termos). O Certbot altera o Nginx para escutar na porta 443 e usar o certificado. Renovação é automática.

Depois disso, acesse `https://polybtc5m.duckdns.org`.

---

## Passo 9: Firewall (opcional)

Para liberar só SSH, HTTP e HTTPS:

```bash
ufw allow 22
ufw allow 80
ufw allow 443
ufw enable
ufw status
```

---

## Vários usuários ao mesmo tempo

Cada usuário logado pode **iniciar e parar o próprio bot** de forma independente. O servidor mantém **um processo do bot por usuário**: usuário A e usuário B podem ter o bot rodando ao mesmo tempo, cada um com sua config e seus logs (`resultados_<user>.txt`, `trades_<user>.jsonl`). Para muitos usuários simultâneos, avalie recurso da VPS (CPU/RAM) e, se necessário, escale com mais instâncias ou um plano maior.

---

## Comandos úteis

| Ação              | Comando                          |
|-------------------|----------------------------------|
| Ver status        | `systemctl status polymarket-bot` |
| Parar             | `systemctl stop polymarket-bot`   |
| Iniciar           | `systemctl start polymarket-bot`  |
| Reiniciar         | `systemctl restart polymarket-bot`|
| Ver logs          | `journalctl -u polymarket-bot -f` |
| Logs do Nginx     | `tail -f /var/log/nginx/error.log` |

---

## Resumo rápido (já com SSH e repositório)

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx
# Opcional, para claim por API (Resgatar agora / Auto-claim): apt install -y software-properties-common && add-apt-repository -y ppa:deadsnakes/ppa && apt update && apt install -y python3.12 python3.12-venv python3.12-dev
cd /root
git clone https://github.com/SEU_USUARIO/BOT_Polymarket.git
cd BOT_Polymarket
python3.12 -m venv venv   # ou: python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
nano .env   # SUPABASE_URL e SUPABASE_ANON_KEY
nano /etc/systemd/system/polymarket-bot.service   # conteúdo do Passo 7
systemctl daemon-reload && systemctl enable --now polymarket-bot
nano /etc/nginx/sites-available/polymarket        # conteúdo do Passo 8.1
ln -sf /etc/nginx/sites-available/polymarket /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
# HTTPS: certbot --nginx -d polybtc5m.duckdns.org
```

---

## Troubleshooting

- **502 Bad Gateway** — O uvicorn não está rodando. Confira `systemctl status polymarket-bot` e `journalctl -u polymarket-bot -n 50`.
- **Página em branco** — Verifique se o caminho do `root` no Nginx aponta para `/root/BOT_Polymarket/frontend` e se existe o arquivo `index.html` dentro dessa pasta.
- **Erro de login / Supabase** — Confira `SUPABASE_URL` e `SUPABASE_ANON_KEY` no `.env` (dentro da pasta do projeto). A URL é a "Project URL" e a chave é a "anon" em **Supabase → Settings → API**. Verifique também se o projeto está ativo e se você executou as migrations (SQL) para criar a tabela `user_config` e as políticas RLS.
- **Bot não inicia** — Cada usuário precisa salvar chave privada e API na aba Config do dashboard. Veja os logs do serviço: `journalctl -u polymarket-bot -f`.
- **Onde fica a pasta do projeto?** — Se você clonou em `/root/BOT_Polymarket`, é essa. O `.env` e o `venv` devem estar dentro dela.
