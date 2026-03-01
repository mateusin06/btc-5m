#!/usr/bin/env python3
"""
Auto-claimer de posições vencedoras na Polymarket.

Usa Playwright para automatizar o claim no navegador.
Execute em background: python auto_claim.py
"""

import asyncio
import os
import sys
from dotenv import load_dotenv

load_dotenv()

POLYMARKET_URL = "https://polymarket.com"


async def run_claimer():
    """Loop de verificação e claim."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("Instale Playwright: pip install playwright && playwright install chromium")
        sys.exit(1)

    # Verificar se há credenciais para login (opcional)
    # O claim pode exigir login na Polymarket
    print("Auto-claimer Polymarket")
    print("Abrindo navegador...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            await page.goto(f"{POLYMARKET_URL}/portfolio")
            print("Acesse polymarket.com/portfolio e faça login se necessário.")
            print("O script irá verificar a página a cada 60s para claims disponíveis.")
            print("Pressione Ctrl+C para encerrar.")

            while True:
                await asyncio.sleep(60)
                # Procurar botão de claim
                claim_btn = await page.query_selector('button:has-text("Claim")')
                if claim_btn:
                    try:
                        await claim_btn.click()
                        print("Claim executado!")
                        await asyncio.sleep(2)
                    except Exception as e:
                        print(f"Erro ao clicar Claim: {e}")

        except KeyboardInterrupt:
            print("\nEncerrado.")
        finally:
            await browser.close()


def main():
    asyncio.run(run_claimer())


if __name__ == "__main__":
    main()
