"""Webhook service: OpenAI SIP call → accept as GPT-Live → sideband → librarian → Telegram report.

Telephony: Odorik (CZ SIP provider) → Asterisk → sip:<project>@sip.api.openai.com. OpenAI then
POSTs `live.transport.incoming` here; we accept with instructions built from persona + memory,
attach to the sideband to serve tool calls and collect the transcript, and after hangup let the
librarian update memory and send the family a summary.
"""

import asyncio
import datetime as dt
import json
import logging
from contextlib import asynccontextmanager

import httpx
import websockets
from fastapi import BackgroundTasks, FastAPI, Request, Response
from openai import AsyncOpenAI

from . import librarian, prompts, tools
from .config import Settings
from .notify import Telegram
from .sideband import ATTACH_URL, CallSession
from .store import Store

log = logging.getLogger(__name__)
INCOMING = {"live.transport.incoming", "live.call.incoming"}  # the latter is the deprecated name


def sip_header(event: dict, name: str) -> str:
    for header in event.get("data", {}).get("sip_headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


class Companion:
    """Everything one deployment needs; one instance per process."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient, openai: AsyncOpenAI):
        self.settings, self.http, self.openai = settings, http, openai
        self.store = Store(settings.data_dir)
        self.telegram = Telegram(http, settings.telegram_token, settings.telegram_chat)
        self.memory_lock = asyncio.Lock()  # ponytail: single process; a shared store would need a DB lock
        self.tools = tools.Toolbox(http, self.store, self.telegram, memory_lock=self.memory_lock)
        self.seen_sessions: set[str] = set()  # webhooks are at-least-once; accept each session only once

    async def session_config(self, mode: prompts.Mode) -> dict:
        try:  # answering the phone must not wait on a slow weather API
            weather = await asyncio.wait_for(self.tools.run("get_weather", None), timeout=3)
        except TimeoutError:
            weather = "neznámé"
        instructions = prompts.companion(
            self.store.load("persona.json", {}),
            self.store.load("memory.json", {}),
            dt.datetime.now(),
            weather,
            mode,
        )
        return {
            "type": "live",
            "model": self.settings.live_model,
            "instructions": instructions,
            "audio": {"output": {"voice": self.settings.voice}},
            "delegation": {
                "type": "responses",
                "responses": {
                    "model": self.settings.backend_model,
                    "instructions": prompts.BACKEND,
                    "tools": tools.SCHEMAS,
                },
            },
        }

    async def handle_call(self, session_id: str, mode: prompts.Mode) -> None:
        started = dt.datetime.now()
        call = CallSession(session_id, self.tools.run)
        try:
            r = await self.http.post(
                f"https://api.openai.com/v1/live/sessions/{session_id}/accept",
                headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                json={"session": await self.session_config(mode)},
            )
            log.info("accept %s mode=%s -> %s", session_id, mode, r.status_code)
            r.raise_for_status()
            headers = {"Authorization": f"Bearer {self.settings.openai_api_key}"}
            async with websockets.connect(
                ATTACH_URL.format(session_id=session_id), additional_headers=headers
            ) as ws:
                await call.run(ws, ws.send)
            log.info("closed %s reason=%s seconds=%s", session_id, call.closed_reason, call.seconds)
        except Exception as e:  # a failed call must be visible to the family, not just a log line
            log.error("call %s failed: %s", session_id, type(e).__name__)
            await self._notify(f"⚠️ Hovor s asistentkou ({mode}, {started:%-d. %-m. %H:%M}) selhal technicky.")
        finally:  # keep whatever was said, even on failure
            self.store.save_transcript(f"{started:%Y-%m-%d_%H%M%S}_{mode}", call.transcript)
        if call.turns:
            kind = "plánovaný" if mode == "scheduled" else "příchozí"
            try:
                await self.after_call(
                    call.transcript, f"{started:%-d. %-m. %H:%M}, {kind}, {round(call.seconds)} s"
                )
            except Exception as e:  # memory stays as it was; the transcript is saved for a re-run
                log.error("librarian failed: %s", type(e).__name__)
                await self._notify(
                    f"Hovor ({kind}, {started:%-d. %-m. %H:%M}) proběhl, shrnutí se nepovedlo."
                )

    async def after_call(self, transcript: str, call_info: str) -> None:
        async with self.memory_lock:  # tools of a concurrent call write live notes into the same file
            current = self.store.load("memory.json", {})
            memory, summary = await librarian.update_memory(
                self.openai, self.settings.librarian_model, current, transcript, dt.date.today()
            )
            self.store.snapshot("memory.json")
            self.store.save("memory.json", memory)
        await self.telegram.send(librarian.report(call_info, summary, memory))

    async def _notify(self, text: str) -> None:
        try:
            await self.telegram.send(text)
        except Exception as e:
            log.error("telegram failed: %s", type(e).__name__)


def create_app(
    settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    if not settings.webhook_secret:
        raise RuntimeError("OPENAI_WEBHOOK_SECRET is required: the webhook is public")
    if not logging.getLogger().handlers:  # uvicorn configures only its own loggers
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include the Telegram token URL
    state: dict[str, Companion] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with httpx.AsyncClient(
            timeout=15, headers={"User-Agent": "senior-voice"}, transport=transport
        ) as http:
            openai = AsyncOpenAI(
                api_key=settings.openai_api_key or "unset", webhook_secret=settings.webhook_secret or None
            )
            state["companion"] = Companion(settings, http, openai)
            yield

    app = FastAPI(title="senior-voice", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.companions = state  # exposed for tests

    @app.post("/webhook")
    async def webhook(request: Request, background: BackgroundTasks) -> Response:
        companion = state["companion"]
        body = await request.body()
        try:  # verify only: the SDK's typed unwrap may not know every live.* event type
            companion.openai.webhooks.verify_signature(body, dict(request.headers))
        except Exception as e:
            log.warning("rejected webhook: %s", e)
            return Response(status_code=400)
        event = json.loads(body)
        log.info("webhook %s", event.get("type"))
        if event.get("type") in INCOMING:
            session_id = event.get("data", {}).get("session_id")
            if not session_id:
                return Response(status_code=400)
            if session_id in companion.seen_sessions:  # a retried delivery of a call we already took
                return Response(status_code=200)
            companion.seen_sessions.add(session_id)
            mode = "scheduled" if "scheduled" in sip_header(event, "X-Mode") else "inbound"
            background.add_task(companion.handle_call, session_id, mode)
        return Response(status_code=200)

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True}

    return app


def main() -> None:  # pragma: no cover - entry point for local runs
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8790)


if __name__ == "__main__":  # pragma: no cover
    main()
