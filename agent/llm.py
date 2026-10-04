"""The "brain" behind the loop, behind one small interface so the model provider can be swapped.

The loop only needs three things from a model:
  step()          -> what the model thinks/says and which tools it wants to call
  add_results()   -> feed tool observations back
  add_user_text() -> nudge it with a plain message

Each provider keeps the conversation in its own native format.
"""
import os
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class Turn:
    thoughts: list = field(default_factory=list)  # reasoning summaries (shown to the human)
    texts: list = field(default_factory=list)     # plain text the model said
    calls: list = field(default_factory=list)     # tool calls to execute
    refused: bool = False


class ClaudeBrain:
    def __init__(self, model: str, effort: str, system: str, tools: list, task: str):
        import anthropic
        self.client = anthropic.Anthropic(max_retries=4)
        self.model, self.effort, self.system, self.tools = model, effort, system, tools
        self.messages = [{"role": "user", "content": task}]

    def step(self) -> Turn:
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        self.messages.append({"role": "assistant", "content": response.content})
        turn = Turn(refused=response.stop_reason == "refusal")
        for b in response.content:
            if b.type == "thinking" and b.thinking:
                turn.thoughts.append(b.thinking)
            elif b.type == "text" and b.text.strip():
                turn.texts.append(b.text)
            elif b.type == "tool_use":
                turn.calls.append(ToolCall(b.id, b.name, b.input))
        return turn

    def add_results(self, results: list):
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": call.id, "content": obs, "is_error": err}
            for call, obs, err in results]})

    def add_user_text(self, text: str):
        self.messages.append({"role": "user", "content": text})


class GeminiBrain:
    def __init__(self, model: str, effort: str, system: str, tools: list, task: str):
        from google import genai
        from google.genai import types
        self.types = types
        self.client = genai.Client()  # reads GEMINI_API_KEY
        self.model = model
        self.unavailable = set()
        declarations = []
        for t in tools:
            d = {"name": t["name"], "description": t["description"]}
            if t["input_schema"].get("properties"):
                d["parameters"] = t["input_schema"]
            declarations.append(d)
        self.config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=declarations)],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=types.ThinkingConfig(include_thoughts=True),
        )
        self.contents = [types.Content(role="user", parts=[types.Part.from_text(text=task)])]

    # Free-tier quotas are per model, so a chain of models keeps the agent running.
    FALLBACK_CHAIN = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
                      "gemini-3.1-flash-lite", "gemini-flash-lite-latest"]

    def _generate(self):
        """503 (overloaded): back off and retry. 429 (quota used up): move to the next model."""
        import httpx
        from google.genai import errors
        busy_retries = net_retries = 0
        while True:
            try:
                return self.client.models.generate_content(model=self.model, contents=self.contents, config=self.config)
            except httpx.TransportError as exc:  # dropped connection / timeout: retry the same model
                net_retries += 1
                if net_retries > 3:
                    raise
                print(f"  ~~ network error ({type(exc).__name__}); retrying in {3 * net_retries}s")
                time.sleep(3 * net_retries)
            except errors.APIError as exc:
                if exc.code in (500, 503) and busy_retries < 2:
                    busy_retries += 1
                    print(f"  ~~ {self.model} busy ({exc.code}); retrying in {5 * busy_retries}s")
                    time.sleep(5 * busy_retries)
                    continue
                if exc.code not in (429, 500, 503):
                    raise
                self.unavailable.add(self.model)  # remembered for the rest of the run
                remaining = [m for m in self.FALLBACK_CHAIN if m not in self.unavailable]
                if not remaining:
                    raise
                print(f"  ~~ {self.model} unavailable ({exc.code}); switching to {remaining[0]}")
                self.model, busy_retries = remaining[0], 0

    def step(self) -> Turn:
        response = self._generate()
        candidate = response.candidates[0]
        content = candidate.content or self.types.Content(role="model", parts=[])
        self.contents.append(content)  # kept as-is so thought signatures are preserved
        turn = Turn(refused=str(candidate.finish_reason).endswith("SAFETY"))
        for i, part in enumerate(content.parts or []):
            if part.function_call:
                fc = part.function_call
                turn.calls.append(ToolCall(fc.id or f"call_{len(self.contents)}_{i}", fc.name, dict(fc.args or {})))
            elif part.text and part.text.strip():
                (turn.thoughts if part.thought else turn.texts).append(part.text)
        return turn

    def add_results(self, results: list):
        parts = [self.types.Part.from_function_response(
                     name=call.name, response={"error": obs} if err else {"result": obs})
                 for call, obs, err in results]
        self.contents.append(self.types.Content(role="user", parts=parts))

    def add_user_text(self, text: str):
        self.contents.append(self.types.Content(role="user", parts=[self.types.Part.from_text(text=text)]))


def make_brain(model: str, **kwargs):
    if model.startswith("gemini"):
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("Set GEMINI_API_KEY first.")
        return GeminiBrain(model, **kwargs)
    return ClaudeBrain(model, **kwargs)
