from __future__ import annotations

import pytest

from octomate import Octomate
from octomate.web.dev_ui import build_dev_ui_router


class FakeAgent:
    model = None


def test_dev_ui_router_is_bound_to_octomate_instance() -> None:
    octomate = Octomate()

    with pytest.raises(ValueError, match="registered agent"):
        build_dev_ui_router(octomate, agent_id="inkling")

    octomate.register_agent("inkling", FakeAgent())
    octomate.include_router(build_dev_ui_router(octomate, agent_id="inkling"))

    app = octomate.app()
    paths = {route.path for route in app.routes}
    assert "/api/chat" in paths
    assert "/api/configure" in paths
