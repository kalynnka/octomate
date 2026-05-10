import os

from pydantic_ai import Agent
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.tools import DeferredToolRequests

from octomate.schemas.actions import AgentMessage
from octomate.tentacles.agent.inkling.graph import InklingOutput
from octomate.tentacles.agent.inkling.prompts import SYSTEM_PROMPT
from octomate.tentacles.agent.inkling.tools import inkling_toolset


def build_inkling_agent() -> Agent[None, InklingOutput]:
    model = GoogleModel(
        "gemini-3-flash-preview",
        provider=GoogleProvider(api_key=os.environ["GEMINI_API_KEY"]),
    )
    return Agent(
        model,
        deps_type=type(None),
        output_type=[list[AgentMessage], DeferredToolRequests],
        toolsets=[inkling_toolset],
        system_prompt=SYSTEM_PROMPT,
    )
