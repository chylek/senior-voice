"""Text-mode simulation of a call: companion model ↔ simulated senior, with stubbed tools.

GPT-Live itself only runs over SIP/WebRTC audio, so this is a *proxy*: the same instructions,
care guide and tool schemas on a text model. It catches prompt and tool-policy regressions
cheaply; voice-specific behaviour (turn-taking, accent, latency) is judged on real call
transcripts instead (`senior-voice-eval transcripts`).
"""

import json
from dataclasses import dataclass, field

from openai import AsyncOpenAI

from .. import prompts, tools
from ..store import Store
from .scenarios import Scenario

STUB_OUTPUTS = {
    "get_weather": "Plzeň dnes: zataženo, 2 až 6 °C, pravděpodobnost srážek 20 %. Právě teď 4 °C.",
    "get_news": "Zprávy ČT24:\n- Vláda schválila rozpočet na příští rok.\n- V Krkonoších napadl první sníh.",
    "get_tv": "ČT1 20:10 StarDance — taneční show\nNova 19:30 Televizní noviny — zpravodajství",
    "log_medication": "zapsáno",
    "remember": "zapamatováno",
    "escalate": "rodina byla upozorněna",
}


@dataclass
class Simulation:
    scenario: Scenario
    turns: list[tuple[str, str]] = field(default_factory=list)
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    context: str = ""  # what the companion knew up front, so the judge doesn't flag it as invented

    @property
    def transcript(self) -> str:
        return "\n".join(f"{who}: {text}" for who, text in self.turns)

    @property
    def missing_tools(self) -> set[str]:
        return set(self.scenario.expect_tools) - {name for name, _ in self.tool_calls}


async def _companion_turn(
    client: AsyncOpenAI, model: str, sim: Simulation, prev: str | None, said: str, ins: str
):
    response = await client.responses.create(
        model=model, instructions=ins, tools=tools.SCHEMAS, previous_response_id=prev, input=said
    )
    while calls := [item for item in response.output if item.type == "function_call"]:
        outputs = []
        for call in calls:
            sim.tool_calls.append((call.name, json.loads(call.arguments or "{}")))
            sim.turns.append(("Nástroj", f"[{call.name} {call.arguments}] {STUB_OUTPUTS[call.name]}"))
            outputs.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": STUB_OUTPUTS[call.name]}
            )
        response = await client.responses.create(
            model=model,
            instructions=ins,
            tools=tools.SCHEMAS,
            previous_response_id=response.id,
            input=outputs,
        )
    return response


async def simulate(
    client: AsyncOpenAI,
    scenario: Scenario,
    companion_model: str,
    senior_model: str,
    store: Store,
    turns: int = 8,
) -> Simulation:
    persona = store.load("persona.json", {})
    meds = ", ".join(f"{m['název']} {m['kdy']}" for m in persona.get("person", {}).get("léky", []))
    sim = Simulation(
        scenario, context=f"{scenario.now:%-d. %-m. %Y %H:%M}; {STUB_OUTPUTS['get_weather']} Léky: {meds}"
    )
    instructions = prompts.companion(persona, {}, scenario.now, STUB_OUTPUTS["get_weather"], scenario.mode)
    said, companion_prev, senior_prev = "Haló?", None, None  # the senior picks up the phone
    sim.turns.append(("Senior", said))
    for _ in range(turns):
        reply = await _companion_turn(client, companion_model, sim, companion_prev, said, instructions)
        companion_prev = reply.id
        sim.turns.append(("Asistentka", reply.output_text))
        senior = await client.responses.create(
            model=senior_model,
            instructions=scenario.brief,
            previous_response_id=senior_prev,
            input=reply.output_text,
        )
        senior_prev, said = senior.id, senior.output_text
        sim.turns.append(("Senior", said))
    return sim
