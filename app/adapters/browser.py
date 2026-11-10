import asyncio
from abc import ABC
from abc import abstractmethod
from typing import override
from urllib.parse import urlsplit

from playwright.async_api import Browser
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright
from playwright.async_api._generated import SetCookieParam

from app import logging
from app.models import Model
from app.result import OnSuccess
from app.result import ServiceError

logger = logging.get_logger(__name__)

_NAVIGATION_TIMEOUT_MS = 30_000
_SCROLL_PAUSE_SECONDS = 1.2
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


class BrowserError(ServiceError):
    NAVIGATION = "navigation"
    TIMEOUT = "timeout"
    SCRIPT = "script"

    @override
    def service(self) -> str:
        return "browser"


class RenderedPage(Model):
    url: str
    final_url: str
    html: str
    extracted: object = None


class ImplementsBrowser(ABC):
    @abstractmethod
    async def render(
        self,
        url: str,
        *,
        cookie_header: str,
        script: str | None = None,
        scrolls: int = 0,
    ) -> OnSuccess[RenderedPage]:
        """Loads the page with the cookies, scrolls, then evaluates `script` (a JS
        expression) and returns its JSON-serialisable value in `extracted`."""

    @abstractmethod
    async def close(self) -> None: ...


class PlaywrightBrowser(ImplementsBrowser):
    __slots__ = ("_browser", "_lock", "_playwright")

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    @override
    async def render(
        self,
        url: str,
        *,
        cookie_header: str,
        script: str | None = None,
        scrolls: int = 0,
    ) -> OnSuccess[RenderedPage]:
        browser = await self._launched()
        context = await browser.new_context(user_agent=_USER_AGENT, locale="en-GB")
        await context.add_cookies(_cookies_for(url, cookie_header))

        try:
            page = await context.new_page()
            await page.goto(
                url, timeout=_NAVIGATION_TIMEOUT_MS, wait_until="networkidle"
            )

            for _ in range(scrolls):
                await page.mouse.wheel(0, 2400)
                await asyncio.sleep(_SCROLL_PAUSE_SECONDS)

            html = await page.content()
            extracted = None if script is None else await page.evaluate(script)

            return RenderedPage(
                url=url, final_url=page.url, html=html, extracted=extracted
            )
        except PlaywrightTimeoutError:
            logger.warning("Browser navigation timed out.", extra={"url": url})

            return BrowserError.TIMEOUT
        except PlaywrightError:
            logger.exception("Browser rendering failed.", extra={"url": url})

            return BrowserError.NAVIGATION if script is None else BrowserError.SCRIPT
        finally:
            await context.close()

    @override
    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None

        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def _launched(self) -> Browser:
        async with self._lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                # NOTE: Chromium's sandbox needs unprivileged user namespaces, which the
                # host forbids inside a no-new-privileges container (tested 2026-11-10);
                # the browser only starts when a session cookie is configured.
                self._browser = await self._playwright.chromium.launch(headless=True)

            return self._browser


def _cookies_for(url: str, cookie_header: str) -> list[SetCookieParam]:
    host = urlsplit(url).hostname or ""
    domain = host.removeprefix("www.")
    cookies: list[SetCookieParam] = []

    for pair in cookie_header.split(";"):
        name, separator, value = pair.strip().partition("=")

        if separator == "" or name == "":
            continue

        cookies.append(
            SetCookieParam(name=name, value=value, domain=f".{domain}", path="/")
        )

    return cookies


def default() -> ImplementsBrowser:
    return PlaywrightBrowser()
