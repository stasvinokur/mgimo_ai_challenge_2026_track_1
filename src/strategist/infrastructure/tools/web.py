"""Веб-загрузчик только по allowlist (config/allowlist.yaml): снимок страницы с датой и хешем.

Запрос вне списка отклоняется до сети; редирект на чужой домен — тоже (каждый переход проверяется
отдельно, автоматическое следование редиректам отключено). Размер ответа ограничен max_bytes и при
чтении потока, а не только по Content-Length. Текст страницы — недоверенные данные.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from strategist.application.errors import NotAllowedError, StrategistError
from strategist.application.ports import Clock, WebSnapshot
from strategist.infrastructure.tools.html_text import decode_html, html_title, main_text_paragraphs

log = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 3_000_000
DEFAULT_TIMEOUT = 20.0
DEFAULT_USER_AGENT = "strategist-research-bot/0.1"
MAX_REDIRECTS = 5
_HTML_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")


class WebFetchError(StrategistError):
    """Страница не получена: HTTP не 2xx, превышен размер, неподдерживаемый тип или сбой сети."""


class AllowlistWebFetcher:
    def __init__(
        self, allowlist: dict[str, Any], clock: Clock, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.domains = {
            str(d).strip().lower().rstrip(".").removeprefix("www.")
            for d in allowlist.get("domains", []) or []
            if str(d).strip()
        }
        self.allow_subdomains = bool(allowlist.get("allow_subdomains", True))
        self.max_bytes = int(allowlist.get("max_bytes") or DEFAULT_MAX_BYTES)
        self.timeout = float(allowlist.get("timeout_seconds") or DEFAULT_TIMEOUT)
        self.user_agent = str(allowlist.get("user_agent") or DEFAULT_USER_AGENT)
        self.clock = clock
        self.transport = transport

    def allowed(self, url: str) -> bool:
        try:
            parts = urlsplit(url.strip())
            host = (parts.hostname or "").lower().rstrip(".")
        except ValueError:
            return False
        if parts.scheme not in ("http", "https") or not host:
            return False
        if host in self.domains or host.removeprefix("www.") in self.domains:
            return True
        return self.allow_subdomains and any(host.endswith("." + d) for d in self.domains)

    def _check(self, url: str, *, redirected_from: str | None = None) -> None:
        if self.allowed(url):
            return
        if redirected_from:
            log.warning("Редирект %s → %s вне allowlist — отклонён", redirected_from, url)
            raise NotAllowedError(f"Редирект на {url} вне allowlist (исходный адрес {redirected_from})")
        log.warning("Запрос %s вне allowlist — отклонён", url)
        raise NotAllowedError(f"{url}: домен вне allowlist (config/allowlist.yaml)")

    def _read_limited(self, response: httpx.Response, url: str) -> bytes:
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            raise WebFetchError(f"{url}: ответ {declared} байт больше лимита {self.max_bytes}")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self.max_bytes:
                raise WebFetchError(f"{url}: ответ больше лимита {self.max_bytes} байт")
            chunks.append(chunk)
        return b"".join(chunks)

    def fetch(self, url: str) -> WebSnapshot:
        self._check(url)
        current = url
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9",
        }
        try:
            with httpx.Client(
                transport=self.transport, timeout=self.timeout, headers=headers, follow_redirects=False
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            target = urljoin(current, location)
                            self._check(target, redirected_from=current)
                            current = target
                            continue
                        if not response.is_success:
                            raise WebFetchError(f"{current}: HTTP {response.status_code}")
                        data = self._read_limited(response, current)
                        return self._snapshot(url, current, response, data)
                raise WebFetchError(f"{url}: больше {MAX_REDIRECTS} редиректов")
        except httpx.HTTPError as exc:
            raise WebFetchError(f"{current}: {type(exc).__name__}: {exc}") from exc

    def _snapshot(self, url: str, final_url: str, response: httpx.Response, data: bytes) -> WebSnapshot:
        content_type = response.headers.get("content-type", "")
        mime = content_type.split(";")[0].strip().lower()
        if mime and mime not in _HTML_TYPES and not mime.startswith("text/"):
            raise WebFetchError(
                f"{final_url}: тип содержимого {mime} не поддерживается (нужен HTML или текст)"
            )
        decoded = decode_html(data, content_type)
        if mime == "text/plain":
            paragraphs = [line.strip() for line in decoded.splitlines() if line.strip()]
            title = paragraphs[0][:200] if paragraphs else ""
        else:
            paragraphs = main_text_paragraphs(decoded)
            title = html_title(decoded)
        # Абзацы через пустую строку: снимок сохраняется как markdown и режется парсером по абзацам.
        text = "\n\n".join(paragraphs)
        return WebSnapshot(
            url=url,
            final_url=final_url,
            status=response.status_code,
            fetched_at=self.clock.now(),
            title=title,
            text=text,
            sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
