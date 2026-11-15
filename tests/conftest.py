import datetime as dt
import json
import os
import time

import httpx
import pytest

from senior_voice.notify import Telegram
from senior_voice.store import Store
from senior_voice.tools import Toolbox

os.environ["TZ"] = "Europe/Prague"  # the service runs in Prague time; keep tests independent of the host
time.tzset()

NOW = dt.datetime(2026, 11, 15, 17, 30).astimezone()

RSS = """<rss><channel>
<item><title>První zpráva</title><description><![CDATA[<p>Text&nbsp;s <b>HTML</b></p>]]></description></item>
<item><title>Druhá zpráva</title><description>Bez formátování</description></item>
<item><title>Třetí zpráva</title><description>Navíc</description></item>
</channel></rss>"""

WEATHER = {
    "current": {"temperature_2m": 3.4, "weather_code": 3},
    "daily": {
        "weather_code": [3, 61, 0],
        "temperature_2m_min": [-1.2, 1, 2],
        "temperature_2m_max": [5.6, 7, 8],
        "precipitation_probability_max": [10, 80, 0],
    },
}


def tv_listing(day: dt.datetime) -> dict:
    def show(h: int, m: int, title: str, length: int = 60) -> dict:
        start = day.replace(hour=h, minute=m).astimezone(dt.UTC)
        stop = start + dt.timedelta(minutes=length)
        return {"title": title, "start": start.isoformat(), "stop": stop.isoformat(), "description": "Popis"}

    return {
        "1": [show(16, 0, "Odpolední film"), show(20, 10, "StarDance")],
        "3": [show(19, 30, "Televizní noviny")],
    }


class FakeWeb:
    """httpx transport serving canned responses and recording requests."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        if host == "api.open-meteo.com":
            return httpx.Response(200, json=WEATHER)
        if host == "ct24.ceskatelevize.cz":
            return httpx.Response(200, content=RSS.encode())
        if host == "tvprogram.centrum.cz":
            return httpx.Response(200, json=tv_listing(NOW))
        if host == "api.telegram.org":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    def sent_telegrams(self) -> list[str]:
        return [
            dict(httpx.QueryParams(r.content.decode()))["text"]
            for r in self.requests
            if r.url.host == "api.telegram.org"
        ]


@pytest.fixture
def web() -> FakeWeb:
    return FakeWeb()


@pytest.fixture
async def http(web):
    async with httpx.AsyncClient(transport=httpx.MockTransport(web)) as client:
        yield client


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path)


@pytest.fixture
def toolbox(http, store) -> Toolbox:
    return Toolbox(http, store, Telegram(http, "TOKEN", "42"), now=lambda: NOW)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
