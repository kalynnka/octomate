"""Exercise an installed CLI or server without loading a source checkout."""

import importlib.util
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

from octomate_protocol import deployment, stream

assert "octomate_cli" not in sys.modules
assert "octomate" not in sys.modules
assert deployment.DatabaseBackup.__module__ == "octomate_protocol.deployment"
assert stream.STREAM_PROTOCOL >= 1
executable = Path(sys.executable).parent / "octomate"
for command in (["--version"], ["--help"], ["serve", "--help"], ["upgrade", "--help"]):
    subprocess.run([str(executable), *command], check=True, stdout=subprocess.DEVNULL)

if sys.argv[1] == "client":
    for package in ("octomate", "fastapi", "uvicorn", "alembic", "sqlalchemy"):
        assert importlib.util.find_spec(package) is None, package
    from octomate_cli.streaming import deepseek, files

    assert "site-packages" in str(files.__file__)
    assert "site-packages" in str(deepseek.__file__)
    print("Client installation imports both transports without server dependencies.")
else:
    with tempfile.TemporaryDirectory(prefix="octomate-installed-") as temporary:
        directory = Path(temporary).resolve()
        config = directory / "config"
        config.mkdir()
        (config / "users.yaml").write_text(
            "users:\n  tester:\n    secret: release-check\n"
        )
        database = directory / "test.sqlite3"
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OCTOMATE", "PYTHONPATH"))
        }
        environment.update(
            {
                "OCTOMATE_HOME": str(config),
                "OCTOMATE_DB_URL": f"sqlite+aiosqlite:///{database}",
                "OCTOMATE__PORT": str(port),
            }
        )
        command = [sys.executable, "-I", "-m", "octomate.deployment"]
        backup = subprocess.check_output(
            [*command, "backup"], cwd=directory, env=environment, text=True
        )
        resolved = deployment.DatabaseBackup.model_validate_json(backup)
        assert resolved.database == database
        assert resolved.backup is None
        assert database.parent == directory
        assert not database.exists()
        subprocess.run(
            [*command, "migrate"],
            cwd=directory,
            env=environment,
            input=backup,
            text=True,
            check=True,
        )
        assert database.is_file()
        with (directory / "server.log").open("w+") as log:
            server = subprocess.Popen(
                [str(executable), "serve"],
                cwd=directory,
                env=environment,
                stdout=log,
                stderr=log,
            )
            try:
                subprocess.run(
                    [*command, "verify"], cwd=directory, env=environment, check=True
                )
            except subprocess.CalledProcessError:
                log.seek(0)
                print(log.read())
                raise
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
        print(
            "Installed server started, migrated a disposable database, and passed authenticated MCP verification."
        )
