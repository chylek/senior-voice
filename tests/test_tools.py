import json

from senior_voice.tools import SCHEMAS, Toolbox, strip_html


async def test_weather_today_includes_current_conditions(toolbox):
    out = await toolbox.get_weather()
    assert out.startswith("Plzeň dnes: zataženo, -1 až 6 °C, pravděpodobnost srážek 10 %.")
    assert "Právě teď 3 °C, zataženo." in out


async def test_weather_tomorrow_has_no_current_part(toolbox):
    out = await toolbox.get_weather("zítra")
    assert "slabý déšť, 1 až 7 °C" in out and "Právě teď" not in out


async def test_news_strips_html_and_limits_count(toolbox):
    out = await toolbox.get_news(2)
    assert out == "Zprávy ČT24:\n- První zpráva: Text\xa0s HTML\n- Druhá zpráva: Bez formátování"


async def test_tv_evening_window_and_channel_names(toolbox, web):
    out = await toolbox.get_tv(when="večer")
    assert out.splitlines() == ["ČT1 20:10 StarDance — Popis", "Nova 19:30 Televizní noviny — Popis"]
    assert web.requests[-1].url.params.get_list("channels[]") == ["1", "2", "3", "4"]


async def test_tv_single_channel(toolbox, web):
    await toolbox.get_tv("Nova", "odpoledne")
    assert web.requests[-1].url.params.get_list("channels[]") == ["3"]


async def test_medication_and_memory_are_persisted(toolbox, store):
    assert await toolbox.run("log_medication", '{"name": "Euthyrox", "taken": true}') == "zapsáno"
    assert store.load("med_log.json", [])[0] == {
        "at": "2026-11-15T17:30+01:00",
        "name": "Euthyrox",
        "taken": True,
        "note": "",
    }
    await toolbox.run("remember", json.dumps({"kind": "fakt", "text": "Má ráda Gotta"}))
    assert store.load("memory.json", {})["live_notes"][-1]["text"] == "Má ráda Gotta"


async def test_escalate_alerts_family_on_telegram(toolbox, web):
    out = await toolbox.run("escalate", json.dumps({"level": "urgentní", "reason": "bolest na hrudi"}))
    assert out == "rodina byla upozorněna"
    assert web.sent_telegrams() == ["⚠️ Hlasová asistentka (urgentní): bolest na hrudi"]


async def test_escalate_without_telegram_still_answers(http, store):
    from senior_voice.notify import Telegram

    tb = Toolbox(http, store, Telegram(http, "", ""))
    assert "Telegram není nastaven" in await tb.run("escalate", '{"level": "info", "reason": "x"}')


async def test_tool_errors_become_text(toolbox):
    assert (await toolbox.run("get_weather", '{"bogus": 1}')).startswith("Nástroj get_weather selhal")


def test_every_schema_has_an_implementation(toolbox):
    assert {s["name"] for s in SCHEMAS} == set(toolbox._impl)


def test_strip_html():
    assert strip_html("<p>a&amp;b</p>") == "a&b"
