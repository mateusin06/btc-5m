# Rodar o projeto no seu PC (localhost)

Guia para quem **não tem Python instalado** e vai testar o dashboard + bot no Windows. Siga na ordem.

---

## 1. Instalar o Python

1. Acesse: **https://www.python.org/downloads/**
2. Baixe a versão **Python 3.12** (ou 3.11) para Windows.
3. Execute o instalador.
4. **Importante:** na primeira tela, marque **"Add python.exe to PATH"**.
5. Clique em **"Install Now"** e termine a instalação.
6. Feche e abra de novo o **PowerShell** ou **Prompt de Comando** (para reconhecer o Python).

Para conferir se instalou:

```powershell
python --version
```

Deve aparecer algo como `Python 3.12.x`.

---

## 2. Entrar na pasta do projeto

Você deve ter recebido a pasta do projeto (por exemplo **BOT_Polymarket**). Abra o PowerShell e vá até ela:

```powershell
cd C:\Users\SEU_USUARIO\Downloads\BOT_Polymarket
```

Troque `SEU_USUARIO` e o caminho pelo local onde está a pasta (pode arrastar a pasta para a janela do PowerShell para colar o caminho).

---

## 3. Criar o ambiente virtual e instalar dependências

No PowerShell, ainda dentro da pasta do projeto:

```powershell
python -m venv venv
```

Depois ative o ambiente:

```powershell
.\venv\Scripts\Activate.ps1
```

Se der erro de política de execução, rode antes:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

E tente de novo `.\venv\Scripts\Activate.ps1`.

Em seguida instale os pacotes:

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

Aguarde terminar.

---

## 4. Subir o dashboard

Com o ambiente ainda ativado (deve aparecer `(venv)` no início da linha), rode:

```powershell
python web.py
```

Deve aparecer algo como:

```
INFO:     Uvicorn running on http://0.0.0.0:8000
```

---

## 5. Abrir no navegador

Abra o navegador e acesse:

**http://localhost:8000**

Você verá a tela de login do dashboard. Crie uma conta ou peça um usuário de teste ao dono do projeto. Depois de logar, preencha a **Config** (chave privada e API da Polymarket, se for testar o bot) e use **Iniciar bot** ou **Resgatar agora** / **Ativar autoclaim** conforme quiser.

---

## Resumo dos comandos (em ordem)

```powershell
python --version
cd C:\CAMINHO\PARA\BOT_Polymarket
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -r requirements.txt
```

Defina `SUPABASE_URL` e `SUPABASE_ANON_KEY` (variáveis de ambiente ou arquivo `.env`, se usar). Depois:

```powershell
python web.py
```

Abrir **http://localhost:8000** no navegador.

Para **parar** o servidor: no PowerShell onde está rodando o `python web.py`, pressione **Ctrl+C**.

---

## Se for Mac ou Linux

Use o **Terminal** e, na pasta do projeto:

```bash
python3 --version
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Defina `SUPABASE_URL` e `SUPABASE_ANON_KEY` (variáveis de ambiente ou .env). Depois:

```bash
python web.py
```

Acesse **http://localhost:8000** no navegador.
