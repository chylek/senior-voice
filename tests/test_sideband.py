import json

from senior_voice.sideband import CallSession


def function_call(call_id: str, name: str, args: dict) -> dict:
    item = {"type": "function_call", "call_id": call_id, "name": name, "arguments": json.dumps(args)}
    return {"type": "response.event", "event": {"type": "response.output_item.done", "item": item}}


async def test_transcript_merges_deltas_and_skips_replays():
    call = CallSession("s1", run_tool=None)
    events = [
        {"type": "session.input_transcript.delta", "event_id": "e1", "delta": "Dobrý "},
        {"type": "session.input_transcript.delta", "event_id": "e2", "delta": "den."},
        {"type": "session.input_transcript.delta", "event_id": "e2", "delta": "den."},  # replay after attach
        {"type": "session.output_transcript.delta", "event_id": "e3", "delta": "Dobrý den, paní Marie."},
    ]
    for e in events:
        assert await call.handle(e) == []
    assert call.transcript == "Senior: Dobrý den.\nAsistentka: Dobrý den, paní Marie."


async def test_function_call_runs_tool_once_and_resumes_backend():
    calls = []

    async def run_tool(name, arguments):
        calls.append((name, json.loads(arguments)))
        return "Plzeň dnes: jasno"

    call = CallSession("s1", run_tool)
    out = await call.handle({**function_call("c1", "get_weather", {"day": "dnes"}), "event_id": "e1"})
    assert calls == [("get_weather", {"day": "dnes"})]
    assert out[0]["item"] == {"type": "function_call_output", "call_id": "c1", "output": "Plzeň dnes: jasno"}
    assert out[1]["type"] == "response.create"
    # the same call_id seen again (e.g. in a lifecycle snapshot) must not run the tool twice
    assert await call.handle({**function_call("c1", "get_weather", {}), "event_id": "e2"}) == []
    assert len(calls) == 1


async def test_other_backend_events_are_ignored():
    call = CallSession("s1", run_tool=None)
    inner = {"type": "response.output_item.done", "item": {"type": "message"}}
    assert await call.handle({"type": "response.event", "event": inner}) == []


async def test_run_stops_on_close_and_sends_tool_results():
    async def run_tool(name, arguments):
        return "ok"

    async def messages():
        yield json.dumps(function_call("c1", "get_news", {}))
        yield json.dumps({"type": "session.closed", "reason": "remote_hangup", "usage": {"seconds": 117.0}})
        yield json.dumps({"type": "session.input_transcript.delta", "delta": "never read"})

    sent = []

    async def send(raw):
        sent.append(json.loads(raw)["type"])

    call = CallSession("s1", run_tool)
    await call.run(messages(), send)
    assert sent == ["response.item.create", "response.create"]
    assert (call.closed_reason, call.seconds) == ("remote_hangup", 117.0)
    assert "never read" not in call.transcript
