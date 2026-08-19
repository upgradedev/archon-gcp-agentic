"""Offline test doubles for the ADK model layer.

Carried across from the frozen Archon GCP build (disclosed in the README under
Pre-existing components) and unchanged in substance: these subclass ADK's
`BaseLlm` and replay scripted responses, so the real ADK agent and the real
SequentialAgent run end to end in CI with no key, no network and no spend.

That matters more here than it did there. This entry's claim is that an agent
performs a chore unattended, and a test that stubs out the agent proves nothing
about it. With these, the ADK function-calling loop and the sequential state
hand-off are genuinely exercised; only the weights behind them are fake.

Callers guard the import with `pytest.importorskip("google.adk")`.
"""
from __future__ import annotations

from collections.abc import AsyncGenerator

from google.adk.models import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types


class ScriptedLlm(BaseLlm):
    """A model that replays a fixed list of actions, one per invocation.

    Each action is either:
      ("call", tool_name, args_dict)  → emit a function call
      ("text", "some final text")     → emit a text response (ends the turn)

    ADK re-invokes the model after each tool result, so a turn's actions should
    end with a ("text", ...) action. Once the script is exhausted the model
    returns a terminal text response, which keeps runs from hanging.
    """
    actions: list = []
    _i: int = 0

    def __init__(self, actions, **kw):
        super().__init__(model="scripted-gemini", **kw)
        object.__setattr__(self, "actions", list(actions))
        object.__setattr__(self, "_i", 0)

    async def generate_content_async(self, llm_request, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        if self._i >= len(self.actions):
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text="(script exhausted)")]))
            return
        action = self.actions[self._i]
        object.__setattr__(self, "_i", self._i + 1)
        kind = action[0]
        if kind == "call":
            _, name, args = action
            part = types.Part(function_call=types.FunctionCall(name=name, args=dict(args)))
            yield LlmResponse(content=types.Content(role="model", parts=[part]))
        else:
            yield LlmResponse(content=types.Content(
                role="model", parts=[types.Part(text=action[1])]))


class ScriptedText(BaseLlm):
    """A model that always returns the same single text — for `SequentialAgent`
    sub-agents, where each stage emits one line into shared state (`output_key`)."""
    reply: str = "ok"

    def __init__(self, reply: str, **kw):
        super().__init__(model="scripted-text", **kw)
        object.__setattr__(self, "reply", reply)

    async def generate_content_async(self, llm_request, stream: bool = False
                                     ) -> AsyncGenerator[LlmResponse, None]:
        yield LlmResponse(content=types.Content(
            role="model", parts=[types.Part(text=self.reply)]))
