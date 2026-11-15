from senior_voice.notify import Telegram, chunks


def test_short_text_is_one_chunk():
    assert chunks("ahoj") == ["ahoj"]
    assert chunks("") == []


def test_splits_on_line_breaks_within_limit():
    text = "a\n" * 3000
    parts = chunks(text)
    assert all(len(p) <= 4000 for p in parts)
    assert "".join(parts).count("a") == 3000


def test_hard_split_without_line_breaks():
    parts = chunks("x" * 9000)
    assert [len(p) for p in parts] == [4000, 4000, 1000]


async def test_long_message_is_sent_in_parts(http, web):
    assert await Telegram(http, "T", "1").send("řádek\n" * 1500)
    assert len(web.sent_telegrams()) == 3


async def test_disabled_without_credentials(http, web):
    assert not await Telegram(http, "", "1").send("x")
    assert web.requests == []
