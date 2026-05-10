"""Dev harness: serve the Inkling agent through pydantic-ai's bundled chat UI.

Run from the project root with:

    uv run uvicorn scripts.inkling_web:app --app-dir . --reload --port 8000

Then open http://localhost:8000.

`--app-dir .` is required: uvicorn doesn't add the cwd to `sys.path` by default,
and this project doesn't install `octomate` as an editable package.

Why this script doesn't reuse `build_inkling_agent()` directly:
the production agent's `output_type=[list[AgentMessage], DeferredToolRequests]`
forces the model to deliver its final answer via a synthetic `final_result` tool
call, which the Vercel AI chat UI renders as a tool-result card instead of as
natural chat text. For sandbox-style chatting we swap to `output_type=str` so
the model emits plain `TextPart`s — same prompt, same tools, same deferred-tool
flow, just a chat-friendly final-message shape.
"""

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.agent.inkling.tools import inkling_toolset

agent: Agent[None, str | DeferredToolRequests] = Agent(
    GoogleModel(
        "gemini-3-flash-preview",
        provider=GoogleProvider(location="global"),
    ),
    deps_type=type(None),
    output_type=[str, DeferredToolRequests],
    toolsets=[inkling_toolset],
    system_prompt=SYSTEM_PROMPT,
)
app = agent.to_web()
