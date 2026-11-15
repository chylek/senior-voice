import base64
import hashlib
import hmac
import json
import time

import httpx
from fastapi.testclient import TestClient

from senior_voice.app import Companion, create_app, sip_header
from senior_voice.config import Settings

SECRET_BYTES = b"test-secret-0123456789abcdef"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()


def signed(body: bytes) -> dict:
    msg_id, ts = "msg_1", str(int(time.time()))
    mac = hmac.new(SECRET_BYTES, f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "webhook-id": msg_id,
        "webhook-timestamp": ts,
        "webhook-signature": "v1," + base64.b64encode(mac).decode(),
    }


def incoming(mode: str | None) -> bytes:
    headers = [{"name": "From", "value": "sip:+420@x"}] + (
        [{"name": "X-Mode", "value": mode}] if mode else []
    )
    data = {"type": "sip", "session_id": "live_1", "sip_headers": headers}
    return json.dumps(
        {"object": "event", "id": "evt_1", "type": "live.transport.incoming", "data": data}
    ).encode()


def client(tmp_path, monkeypatch, calls):
    async def fake_handle_call(self, session_id, mode):
        calls.append((session_id, mode))

    monkeypatch.setattr(Companion, "handle_call", fake_handle_call)
    settings = Settings(data_dir=tmp_path, openai_api_key="sk-test", webhook_secret=SECRET)
    return TestClient(create_app(settings, transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def test_signed_incoming_call_is_handled_with_mode(tmp_path, monkeypatch):
    calls = []
    with client(tmp_path, monkeypatch, calls) as c:
        for mode in ("scheduled", None):
            body = incoming(mode)
            assert c.post("/webhook", content=body, headers=signed(body)).status_code == 200
    assert calls == [("live_1", "scheduled"), ("live_1", "inbound")]


def test_unsigned_or_tampered_webhook_is_rejected(tmp_path, monkeypatch):
    calls = []
    with client(tmp_path, monkeypatch, calls) as c:
        body = incoming(None)
        assert c.post("/webhook", content=body).status_code == 400
        assert c.post("/webhook", content=body + b" ", headers=signed(body)).status_code == 400
    assert calls == []


def test_other_events_are_acknowledged_but_ignored(tmp_path, monkeypatch):
    calls = []
    with client(tmp_path, monkeypatch, calls) as c:
        body = json.dumps({"type": "response.completed", "data": {}}).encode()
        assert c.post("/webhook", content=body, headers=signed(body)).status_code == 200
        assert c.get("/health").json() == {"ok": True}
    assert calls == []


def test_sip_header_lookup_is_case_insensitive():
    event = {"data": {"sip_headers": [{"name": "x-mode", "value": "scheduled"}]}}
    assert sip_header(event, "X-Mode") == "scheduled"
    assert sip_header(event, "From") == ""


async def test_session_config_uses_responses_delegation_with_tools(tmp_path, http):
    companion = Companion(Settings(data_dir=tmp_path), http, openai=None)
    config = await companion.session_config("inbound")
    assert config["model"] == "gpt-live-1" and config["type"] == "live"
    delegation = config["delegation"]["responses"]
    assert delegation["model"] == "gpt-6-luna"
    assert {t["name"] for t in delegation["tools"]} >= {"get_weather", "escalate"}
    assert "Plzeň dnes: zataženo" in config["instructions"]
