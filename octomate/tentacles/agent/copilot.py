from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import httpx

from octomate.config import CopilotConfig
from octomate.nerve import SendSegments
from octomate.schemas.events import MessageEvent
from octomate.schemas.segments import TextSegment
from octomate.schemas.session import SessionKey
from octomate.tentacles.agent.base import AgentTentacle

if TYPE_CHECKING:
    from octomate.octopus import Octopus

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
ISSUE_REF_PATTERN = re.compile(
    r"(?:(?P<owner>[\w.\-]+)/(?P<repo>[\w.\-]+))?#(?P<number>\d+)"
    r"|https?://github\.com/(?P<url_owner>[\w.\-]+)/(?P<url_repo>[\w.\-]+)/issues/(?P<url_number>\d+)"
)


class CopilotTentacle(AgentTentacle):
    config: CopilotConfig
    http: httpx.AsyncClient

    def __init__(self, tag: str, octopus: Octopus, config: CopilotConfig) -> None:
        super().__init__(tag, octopus, description=config.description)
        self.config = config
        self.http = httpx.AsyncClient(
            base_url=GITHUB_API,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {config.github_token.get_secret_value()}",
            },
            timeout=30.0,
        )

    async def run(
        self,
        key: SessionKey,
        contents: list[MessageEvent],
        *,
        session_name: str = "",
        silent: bool = False,
    ) -> str | None:
        task = "".join(str(part) for part in contents[0].to_content_parts())

        owner, repo, issue_number = _parse_issue_ref(task, self.config)
        agent_assignment = _build_agent_assignment(self.config, owner, repo)

        try:
            if issue_number:
                resp = await self.http.post(
                    f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
                    json={
                        "assignees": ["copilot-swe-agent[bot]"],
                        "agent_assignment": agent_assignment,
                    },
                )
                resp.raise_for_status()
                issue_url = f"https://github.com/{owner}/{repo}/issues/{issue_number}"
            else:
                title, body = _split_title_body(task)
                resp = await self.http.post(
                    f"/repos/{owner}/{repo}/issues",
                    json={
                        "title": title,
                        "body": body,
                        "assignees": ["copilot-swe-agent[bot]"],
                        "agent_assignment": agent_assignment,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                issue_url = data.get(
                    "html_url", f"https://github.com/{owner}/{repo}/issues"
                )
                issue_number = data.get("number", "?")

            text = (
                f"🤖 Copilot is now working on <{issue_url}|{owner}/{repo}#{issue_number}>.\n"
                f"You'll receive a review request when the PR is ready."
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                "CopilotTentacle: GitHub API error %s: %s",
                exc.response.status_code,
                exc.response.text[:500],
            )
            text = f"⚠️ Failed to assign Copilot: HTTP {exc.response.status_code}"
        except httpx.HTTPError as exc:
            logger.error("CopilotTentacle: HTTP error: %s", exc)
            text = f"⚠️ Failed to reach GitHub API: {exc}"

        if silent:
            return text

        nerve = self.octopus.agent_nerve
        await nerve.send(
            SendSegments(key=key, segments=[TextSegment(data={"text": text})])
        )

    # TODO: poll for PR completion and notify user
    # GitHub doesn't support webhooks without a public endpoint.
    # Future: periodically check /repos/{owner}/{repo}/issues/{number}/timeline
    # for linked PR events, then notify via channel.


def _parse_issue_ref(task: str, config: CopilotConfig) -> tuple[str, str, int | None]:
    match = ISSUE_REF_PATTERN.search(task)
    if not match:
        return config.owner, config.repo, None

    if match.group("url_owner"):
        return (
            match.group("url_owner"),
            match.group("url_repo"),
            int(match.group("url_number")),
        )

    owner = match.group("owner") or config.owner
    repo = match.group("repo") or config.repo
    return owner, repo, int(match.group("number"))


def _build_agent_assignment(config: CopilotConfig, owner: str, repo: str) -> dict:
    assignment: dict[str, str] = {
        "target_repo": f"{owner}/{repo}",
        "base_branch": config.base_branch,
    }
    if config.custom_instructions:
        assignment["custom_instructions"] = config.custom_instructions
    if config.custom_agent:
        assignment["custom_agent"] = config.custom_agent
    if config.model:
        assignment["model"] = config.model
    return assignment


def _split_title_body(task: str) -> tuple[str, str]:
    lines = task.strip().splitlines()
    title = lines[0][:256] if lines else "Copilot task"
    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else title
    return title, body
