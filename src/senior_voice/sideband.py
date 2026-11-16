"""GPT-Live sideband: transcripts in, tool results out.

`CallSession.handle` is a pure-ish event handler (no I/O except tool calls), so the whole
protocol logic is testable with a list of events; `CallSession.run` wires it to the websocket.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ATTACH_URL = "wss://api.openai.com/v1/live/sessions/{session_id}/attach"
SPEAKERS = {"session.input_transcript.delta": "Senior", "session.output_transcript.delta": "Asistentka"}

ToolRunner = Callable[[str, str | None], Awaitable[str]]
TurnHook = Callable[[str, str], Awaitable[list[dict]]]  # (latest senior turn, recent context) -> events


@dataclass
class CallSession:
    session_id: str
    run_tool: ToolRunner
    turns: list[list[str]] = field(default_factory=list)  # [speaker, text], consecutive deltas merged
    closed_reason: str = ""
    seconds: float = 0.0
    on_senior_turn: TurnHook | None = None  # sidecar guard; its events are sent when it finishes
    finished_senior_turns: list[tuple[str, str]] = field(default_factory=list)  # (latest, recent context)
    _seen: set[str] = field(default_factory=set)
    _done_calls: set[str] = field(default_factory=set)
    _guard_tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def transcript(self) -> str:
        return "\n".join(f"{who}: {text.strip()}" for who, text in self.turns)

    def _add(self, who: str, text: str) -> None:
        if self.turns and self.turns[-1][0] == who:
            self.turns[-1][1] += text
            return
        if (
            self.turns and self.turns[-1][0] == "Senior"
        ):  # there is no turn-end event: a speaker change ends it
            recent = "\n".join(f"{w}: {t.strip()}" for w, t in self.turns[-6:])
            self.finished_senior_turns.append((self.turns[-1][1].strip(), recent))
        self.turns.append([who, text])

    async def handle(self, event: dict) -> list[dict]:
        """Process one server event; return client events to send back."""
        event_id = event.get("event_id")
        if event_id in self._seen:  # attach replays the last ~3 s of events
            return []
        if event_id:
            self._seen.add(event_id)
        kind = event.get("type")
        if kind in SPEAKERS:
            self._add(SPEAKERS[kind], event.get("delta", ""))
        elif kind == "response.event":
            return await self._backend_event(event.get("event", {}))
        elif kind == "session.closed":
            self.closed_reason = event.get("reason", "")
            self.seconds = (event.get("usage") or {}).get("seconds", 0.0)
        elif kind == "error":
            log.error("sideband error %s", event.get("error"))
        return []

    async def _backend_event(self, inner: dict) -> list[dict]:
        item = inner.get("item", {})
        if inner.get("type") != "response.output_item.done" or item.get("type") != "function_call":
            return []
        call_id = item["call_id"]
        if call_id in self._done_calls:
            return []
        self._done_calls.add(call_id)
        output = await self.run_tool(item["name"], item.get("arguments"))
        log.info("tool %s(%s) -> %s", item["name"], item.get("arguments"), output[:120])
        self._add("Nástroj", f"[{item['name']} {item.get('arguments')}] {output[:300]}\n")
        return [
            {
                "type": "response.item.create",
                "event_id": f"out_{call_id}",
                "item": {"type": "function_call_output", "call_id": call_id, "output": output},
            },
            {"type": "response.create", "event_id": f"go_{call_id}"},  # required to resume the backend
        ]

    async def _guard(self, latest: str, recent: str, send: Callable[[str], Awaitable[None]]) -> None:
        try:
            for out in await self.on_senior_turn(latest, recent):
                if not self.closed_reason:
                    await send(json.dumps(out))
        except Exception as e:  # the guard is best-effort; never take the call down with it
            log.error("guard hook failed: %s", type(e).__name__)

    async def run(self, messages: AsyncIterable[str], send: Callable[[str], Awaitable[None]]) -> None:
        async for raw in messages:
            for out in await self.handle(json.loads(raw)):
                await send(json.dumps(out))
            while self.on_senior_turn and self.finished_senior_turns:
                # off the event loop's critical path: the conversation goes on while the guard checks
                task = asyncio.create_task(self._guard(*self.finished_senior_turns.pop(0), send))
                self._guard_tasks.add(task)
                task.add_done_callback(self._guard_tasks.discard)
            if self.closed_reason:
                break
        if self.on_senior_turn and self.turns and self.turns[-1][0] == "Senior":  # senior spoke last
            recent = "\n".join(f"{w}: {t.strip()}" for w, t in self.turns[-6:])
            self._guard_tasks.add(asyncio.create_task(self._guard(self.turns[-1][1].strip(), recent, send)))
        if self._guard_tasks:  # let alerts go out even if the caller hung up mid-check
            await asyncio.wait(self._guard_tasks, timeout=10)
