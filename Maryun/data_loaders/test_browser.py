import asyncio
import os
from pyppeteer import launch

if 'data_loader' not in globals():
    from mage_ai.data_preparation.decorators import data_loader
if 'test' not in globals():
    from mage_ai.data_preparation.decorators import test

EXECUTABLE_PATH = os.getenv("EXECUTABLE_PATH", "/usr/bin/google-chrome")


async def run_test():
    browser = await launch(
        headless=True,
        executablePath=EXECUTABLE_PATH if os.path.exists(EXECUTABLE_PATH) else None,
        dumpio=True,
        ignoreHTTPSErrors=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )

    try:
        page = await browser.newPage()
        await page.goto("https://ipinfo.io", {"waitUntil": "domcontentloaded", "timeout": 60000})
        title = await page.title()
        return {
            "success": True,
            "title": title,
            "url": page.url,
        }
    finally:
        await browser.close()


@data_loader
def load_data(*args, **kwargs):
    return asyncio.run(run_test())


@test
def test_output(output, *args):
    assert output is not None