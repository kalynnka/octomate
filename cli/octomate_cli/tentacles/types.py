"""The JSON aliases `octomate.types.json` also defines, mirrored rather than imported:
octomate-cli must not depend on the server package, and `JsonValue` is pydantic's own.

Not named `json`, and no module here may take a stdlib name: `launch.py` and
`emit.py` run by path, which puts this directory first on their `sys.path`, so a
stdlib-named sibling would shadow the real module out from under them."""

from __future__ import annotations

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]
