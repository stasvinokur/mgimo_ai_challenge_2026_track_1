"""AllowlistWebFetcher без сети: httpx.MockTransport вместо настоящих запросов."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
import yaml

from fakes import FixedClock
from strategist.application.errors import NotAllowedError
from strategist.infrastructure.tools.web import AllowlistWebFetcher, WebFetchError

ROOT = Path(__file__).resolve().parents[2]

PAGE = """<html><head><meta charset="utf-8"><title>Рынок связи — новости</title></head><body>
<nav>Главная | Новости | Контакты</nav>
<article><h1>Семейные тарифы растут</h1>
<p>Доля абонентов с семейными пакетами выросла до 18% за год, следует из отчёта компании.</p>
<p>Отток в сегменте семейных тарифов снизился на 2 процентных пункта по сравнению с прошлым годом.</p>
</article>
<footer>© 2026</footer><script>window.track = "не текст страницы";</script>
</body></html>"""


@pytest.fixture
def allowlist():
    return yaml.safe_load((ROOT / "config" / "allowlist.yaml").read_text(encoding="utf-8"))


class Recorder:
    """Обработчик MockTransport: маршруты по URL и журнал запросов."""

    def __init__(self, routes: dict[str, httpx.Response]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get(str(request.url))
        if response is None:
            return httpx.Response(404, text="нет такой страницы")
        return response

    @property
    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]


def fetcher(
    allowlist, routes: dict[str, httpx.Response], **overrides
) -> tuple[AllowlistWebFetcher, Recorder]:
    recorder = Recorder(routes)
    config = {**allowlist, **overrides}
    return AllowlistWebFetcher(config, FixedClock(), transport=httpx.MockTransport(recorder)), recorder


def html(body: str = PAGE, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": "text/html; charset=utf-8", **headers}
    )


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("https://rbc.ru/news/1", True),
        ("https://www.rbc.ru/news/1", True),
        ("https://quote.rbc.ru/x", True),  # поддомен
        ("http://habr.com/ru/articles/1/", True),
        ("https://RU.Wikipedia.org/wiki/МегаФон", True),
        ("https://evilrbc.ru/", False),  # совпадение по суффиксу без точки — не поддомен
        ("https://rbc.ru.evil.com/", False),
        ("https://rbc.ru@evil.com/", False),  # user-info: настоящий хост — evil.com
        ("https://evil.com/?next=https://rbc.ru", False),
        ("ftp://rbc.ru/file", False),
        ("file:///etc/passwd", False),
        ("javascript:alert(1)", False),
        ("rbc.ru/news", False),  # без схемы
    ],
)
def test_allowed(allowlist, url, ok):
    web, _ = fetcher(allowlist, {})
    assert web.allowed(url) is ok


def test_subdomains_can_be_disabled(allowlist):
    web, _ = fetcher(allowlist, {}, allow_subdomains=False)
    assert web.allowed("https://rbc.ru/") and web.allowed("https://www.rbc.ru/")
    assert not web.allowed("https://quote.rbc.ru/")


def test_fetch_outside_allowlist_never_touches_network(allowlist):
    web, recorder = fetcher(allowlist, {"https://evil.com/": html()})
    with pytest.raises(NotAllowedError, match="allowlist"):
        web.fetch("https://evil.com/")
    assert recorder.requests == []


def test_fetch_extracts_main_text(allowlist):
    url = "https://www.comnews.ru/content/1"
    web, recorder = fetcher(allowlist, {url: html()})
    snap = web.fetch(url)
    assert snap.status == 200 and snap.url == url and snap.final_url == url
    assert snap.fetched_at == FixedClock().now()
    assert "выросла до 18% за год" in snap.text
    assert "снизился на 2 процентных пункта" in snap.text
    assert "window.track" not in snap.text and "Контакты" not in snap.text
    assert "\n\n" in snap.text  # абзацы через пустую строку — снимок режется парсером по абзацам
    assert snap.title in ("Семейные тарифы растут", "Рынок связи — новости")
    assert snap.sha256 == hashlib.sha256(snap.text.encode()).hexdigest()
    assert recorder.requests[0].headers["user-agent"] == allowlist["user_agent"]


def test_fallback_without_trafilatura_result(allowlist):
    url = "https://vc.ru/short"
    web, _ = fetcher(
        allowlist, {url: html("<html><body><div>Коротко.</div><script>x()</script></body></html>")}
    )
    assert web.fetch(url).text == "Коротко."


def test_plain_text_and_charset_from_header(allowlist):
    url = "https://cbr.ru/data.txt"
    body = "Ключевая ставка\n\nСтавка сохранена.".encode("cp1251")
    web, _ = fetcher(
        allowlist,
        {
            url: httpx.Response(
                200, content=body, headers={"content-type": "text/plain; charset=windows-1251"}
            )
        },
    )
    snap = web.fetch(url)
    assert snap.text == "Ключевая ставка\n\nСтавка сохранена."
    assert snap.title == "Ключевая ставка"


def test_redirect_inside_allowlist_is_followed(allowlist):
    start, target = "http://rbc.ru/a", "https://www.rbc.ru/a"
    web, recorder = fetcher(
        allowlist, {start: httpx.Response(301, headers={"location": target}), target: html()}
    )
    snap = web.fetch(start)
    assert snap.url == start and snap.final_url == target
    assert recorder.urls == [start, target]


def test_relative_redirect(allowlist):
    start = "https://habr.com/old"
    web, _ = fetcher(
        allowlist, {start: httpx.Response(302, headers={"location": "/new"}), "https://habr.com/new": html()}
    )
    assert web.fetch(start).final_url == "https://habr.com/new"


def test_redirect_outside_allowlist_is_rejected(allowlist):
    start = "https://vc.ru/go"
    web, recorder = fetcher(
        allowlist,
        {
            start: httpx.Response(302, headers={"location": "https://evil.example/steal"}),
            "https://evil.example/steal": html(),
        },
    )
    with pytest.raises(NotAllowedError, match="Редирект"):
        web.fetch(start)
    assert recorder.urls == [start]  # чужой адрес не запрашивался


def test_redirect_loop_is_limited(allowlist):
    url = "https://vc.ru/loop"
    web, recorder = fetcher(allowlist, {url: httpx.Response(302, headers={"location": url})})
    with pytest.raises(WebFetchError, match="редирект"):
        web.fetch(url)
    assert len(recorder.requests) == 6


@pytest.mark.parametrize("status", [404, 500, 403])
def test_only_2xx(allowlist, status):
    url = "https://rbc.ru/missing"
    web, _ = fetcher(allowlist, {url: html(status=status)})
    with pytest.raises(WebFetchError, match=f"HTTP {status}"):
        web.fetch(url)


def test_size_limit_by_content_length(allowlist):
    url = "https://rbc.ru/big"
    web, _ = fetcher(allowlist, {url: html("x" * 2000)}, max_bytes=1000)
    with pytest.raises(WebFetchError, match="лимит"):
        web.fetch(url)


def test_size_limit_while_streaming(allowlist):
    url = "https://rbc.ru/stream"

    def chunks():
        for _ in range(10):
            yield b"<p>" + b"a" * 300 + b"</p>"

    response = httpx.Response(200, content=chunks(), headers={"content-type": "text/html"})
    assert "content-length" not in response.headers
    web, _ = fetcher(allowlist, {url: response}, max_bytes=1000)
    with pytest.raises(WebFetchError, match="лимит"):
        web.fetch(url)


def test_unsupported_content_type(allowlist):
    url = "https://arxiv.org/pdf/1"
    web, _ = fetcher(
        allowlist,
        {url: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})},
    )
    with pytest.raises(WebFetchError, match="application/pdf"):
        web.fetch(url)


def test_network_errors_are_wrapped(allowlist):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout", request=request)

    web = AllowlistWebFetcher(allowlist, FixedClock(), transport=httpx.MockTransport(boom))
    with pytest.raises(WebFetchError, match="ConnectTimeout"):
        web.fetch("https://rbc.ru/slow")
    assert web.timeout == float(allowlist["timeout_seconds"])
