#!/usr/bin/env python3
"""
Auto-claimer de posições vencedoras na Polymarket.

Usa Playwright para automatizar o claim no navegador.
Execute em background: python auto_claim.py

Variáveis de ambiente:
  HEADLESS=1     — roda sem abrir janela (para servidor)
  BOT_USER_ID   — identificador do usuário (para log em data/autoclaim_<id>.txt)
"""

import asyncio
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

POLYMARKET_URL = "https://polymarket.com"


def _safe_user_id(raw: str) -> str:
    """Sanitiza para uso em nome de arquivo."""
    if not raw or not isinstance(raw, str):
        return "default"
    return re.sub(r"[^\w\-]", "", raw)[:64] or "default"


async def run_claimer():
    """Loop de verificação e claim."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Instale Playwright: pip install playwright && playwright install chromium")
        sys.exit(1)

    headless = os.getenv("HEADLESS", "").strip().lower() in ("1", "true", "yes")
    user_id = _safe_user_id(os.getenv("BOT_USER_ID", ""))
    log_path = Path(__file__).resolve().parent / "data" / f"autoclaim_{user_id}.txt"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8")

    def log(msg: str):
        print(msg)
        try:
            log_file.write(msg + "\n")
            log_file.flush()
        except Exception:
            pass

    log("Auto-claimer Polymarket (headless=%s)" % headless)
    log("Abrindo navegador...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(f"{POLYMARKET_URL}/portfolio")
            log("Acesse polymarket.com/portfolio e faça login se necessário.")
            log("O script irá verificar a página a cada 60s para claims disponíveis.")
            log("Pressione Ctrl+C para encerrar.")

            while True:
                await asyncio.sleep(60)
                # Procurar botão de claim
                claim_btn = await page.query_selector('button:has-text("Claim")')
                if claim_btn:
                    try:
                        await claim_btn.click()
                        log("Claim executado!")
                        await asyncio.sleep(2)
                    except Exception as e:
                        log("Erro ao clicar Claim: %s" % e)

        except (KeyboardInterrupt, asyncio.CancelledError):
            log("Encerrado.")
        finally:
            try:
                log_file.close()
            except Exception:
                pass
            await browser.close()


def main():
    asyncio.run(run_claimer())


if __name__ == "__main__":
    main()
