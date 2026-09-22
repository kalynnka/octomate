"""The Claude plugin registers the same events as the direct CLI installer."""

from __future__ import annotations

import json
from pathlib import Path

from octomate_cli.tentacles.claude.hooks import HANDLED_HOOK_EVENTS, HOOK_TIMEOUT

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "octomate-claude"


def test_plugin_bundles_the_existing_hook_events_and_mcp_command() -> None:
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    [entry] = marketplace["plugins"]
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert entry["name"] == manifest["name"] == "octomate"
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == set(HANDLED_HOOK_EVENTS)
    for event, groups in hooks.items():
        [group] = groups
        handlers = group["hooks"]
        assert handlers[0] == {
            "type": "command",
            "command": "octomate-emit --path /hooks/claude",
            "timeout": HOOK_TIMEOUT,
        }
        if event == "UserPromptSubmit":
            assert handlers[1:] == [
                {
                    "type": "command",
                    "command": "octomate-launch --path /hooks/claude --agent claude --octomate octomate",
                    "timeout": HOOK_TIMEOUT,
                }
            ]
        else:
            assert len(handlers) == 1
    assert json.loads((PLUGIN / ".mcp.json").read_text()) == {
        "mcpServers": {"octomate": {"command": "octomate-claude-mcp"}}
    }
