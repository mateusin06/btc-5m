# Deploy 24/7 na VPS (Hostinger ou outra)

Guia passo a passo para rodar o **dashboard e o bot Polymarket** em um VPS Ubuntu (ex.: Hostinger KVM). O servidor fica ligado 24/7; o site e a API respondem o tempo todo. **Vários usuários podem usar ao mesmo tempo**, cada um com seu próprio bot.

---

## O que você vai ter no final

- Um site (ex.: `https://seu-dominio.com`) com login e dashboard.
- Cada usuário faz login, salva as credenciais da Polymarket na aba Config e clica em **Iniciar** para rodar o bot.
- **Vários usuários podem ter o bot rodando ao mesmo tempo** — um processo por pessoa; stats e logs são separados por usuário.

---

## Pré-requisitos

1. **VPS Ubuntu** (Hostinger KVM 1 ou 2 é suficiente para começar; para muitos usuários simultâneos, considere KVM 2 ou superior).
2. **Domínio** apontando para o IP da VPS (opcional mas recomendado para HTTPS).
3. **Conta no Supabase** com o projeto criado e a tabela `user_config` (migrations do projeto).
4. **Acesso SSH** ao VPS (usuário root ou com sudo).

---

## Passo 1: Conectar na VPS

No seu PC (PowerShell ou terminal):

```bash
ssh root@SEU_IP_VPS
```

Substitua `SEU_IP_VPS` pelo IP que a Hostinger forneceu. Se usar chave SSH, o comando é o mesmo; a senha ou a chave será pedida.

---

## Passo 2: Atualizar o sistema e instalar dependências

```bash
apt update && apt upgrade -y
apt install -y python3 python3-pip python3-venv git nginx
```

- **python3 / pip / venv** — para rodar o bot e o backend.
- **git** — para clonar o repositório.
- **nginx** — para servir o site e fazer proxy para a API.

(Opcional, se for usar scripts que usam Playwright: `apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2`.)

---

## Passo 3: Clonar o repositório do bot

Troque a URL pelo seu repositório (GitHub/GitLab):

```bash
cd /root
git clone https://github.com/SEU_USUARIO/BOT_Polymarket.git
cd BOT_Polymarket
```

Se você não usa Git ainda, pode enviar os arquivos por SFTP/SCP para `/root/BOT_Polymarket`.

---

## Passo 4: Ambiente virtual e dependências Python

```bash
python3 -m venv venv
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

Crie o arquivo:

```bash
nano .env
```

Coloque **apenas** o que o dashboard precisa (Supabase). Exemplo:

```env
SUPABASE_URL=https://seu-projeto.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

- **SUPABASE_URL** — URL do projeto no Supabase (Settings → API).
- **SUPABASE_ANON_KEY** — chave “anon” pública do mesmo projeto.

Salve: `Ctrl+O`, Enter, depois `Ctrl+X`.

Não é obrigatório colocar `POLY_*` no `.env` do servidor; cada usuário configura as próprias chaves no site.

---

## Passo 6: Testar na mão

Antes de configurar serviço e Nginx, confira se o app sobe:

```bash
cd /root/BOT_Polymarket
source venv/bin/activate
uvicorn web:app --host 0.0.0.0 --port 8000
```

Acesse no navegador: `http://SEU_IP_VPS:8000`. Deve abrir o dashboard. Pare o servidor com `Ctrl+C`.

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
WorkingDirectory=/root/BOT_Polymarket
Environment=PATH=/root/BOT_Polymarket/venv/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/root/BOT_Polymarket/venv/bin/uvicorn web:app --host 127.0.0.1 --port 8000
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
    server_name _;
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

Se tiver **domínio** (ex.: `bot.seudominio.com`), troque `server_name _;` por:

```nginx
server_name bot.seudominio.com;
```

Ative o site e recarregue o Nginx:

```bash
ln -sf /etc/nginx/sites-available/polymarket /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
```

Acesse `http://SEU_IP` ou `http://bot.seudominio.com`. Deve carregar o dashboard.

### 8.2 HTTPS com Certbot (recomendado)

Instale o Certbot e gere o certificado (use o domínio que aponta para esta VPS):

```bash
apt install -y certbot python3-certbot-nginx
certbot --nginx -d bot.seudominio.com
```

Siga as perguntas (e-mail, aceitar termos). O Certbot altera o Nginx para escutar na porta 443 e usar o certificado. Renovação é automática.

Depois disso, acesse `https://bot.seudominio.com`.

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
cd /root
git clone https://github.com/SEU_USUARIO/BOT_Polymarket.git
cd BOT_Polymarket
python3 -m venv venv
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
# HTTPS: certbot --nginx -d bot.seudominio.com
```

---

## Troubleshooting

- **502 Bad Gateway** — O uvicorn não está rodando. Confira `systemctl status polymarket-bot` e `journalctl -u polymarket-bot -n 50`.
- **Página em branco** — Verifique se o caminho do `root` no Nginx aponta para `/root/BOT_Polymarket/frontend` e se existe `index.html`.
- **Erro de login / Supabase** — Confira `SUPABASE_URL` e `SUPABASE_ANON_KEY` no `.env` e se o projeto Supabase está ativo e com a tabela `user_config` e RLS configurados.
- **Bot não inicia** — Cada usuário precisa salvar chave privada e API na aba Config. Veja os logs do serviço: `journalctl -u polymarket-bot -f`.
