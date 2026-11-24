"""A real HTTP listener with DSH's readiness and configuration contract."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"guest-ready")


patch = Path(sys.argv[sys.argv.index("--patch") + 1])
Path(os.environ["DSH_TEST_LOG"]).write_text(
    json.dumps(
        {
            "pid": os.getpid(),
            "home": os.environ["DSH_HOME"],
            "cwd": os.getcwd(),
            "patch": json.loads(patch.read_text()),
        }
    )
)
server = HTTPServer(("127.0.0.1", 0), Handler)
print(f"dsh web: http://127.0.0.1:{server.server_port}/?token=guest-token", flush=True)
server.serve_forever()
