"""Rebuild source distributions and exercise wheels in isolated installations."""

import subprocess
import sys
import tempfile
import tomllib
from email import message_from_bytes
from pathlib import Path
from zipfile import ZipFile

root = Path(__file__).resolve().parents[2]
dist = root / "dist"
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
for project in ("cli", "protocol"):
    metadata = tomllib.loads((root / project / "pyproject.toml").read_text())["project"]
    assert metadata["version"] == version, project
assert len(list(dist.glob("*.whl"))) == 3
assert len(list(dist.glob("*.tar.gz"))) == 3
for wheel in dist.glob("*.whl"):
    with ZipFile(wheel) as archive:
        metadata_file = next(
            name for name in archive.namelist() if name.endswith("/METADATA")
        )
        metadata = message_from_bytes(archive.read(metadata_file))
        assert metadata["Version"] == version, wheel
        dependencies = set(metadata.get_all("Requires-Dist", []))
        if metadata["Name"] in {"octomate", "octomate-cli"}:
            assert f"octomate-protocol=={version}" in dependencies
        if metadata["Name"] == "octomate":
            assert f"octomate-cli=={version}" in dependencies
            assert "octomate/app.py" in archive.namelist()
            assert "octomate/migrations/alembic.ini" in archive.namelist()
            assert any(
                name.startswith("octomate/migrations/versions/")
                for name in archive.namelist()
            )

with tempfile.TemporaryDirectory(prefix="octomate-distributions-") as temporary:
    directory = Path(temporary)
    constraints = directory / "constraints.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--locked",
            "--no-dev",
            "--no-emit-workspace",
            "--no-hashes",
            "--output-file",
            str(constraints),
        ],
        cwd=root,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for source in sorted(dist.glob("*.tar.gz")):
        subprocess.run(
            [
                "uv",
                "build",
                "--wheel",
                "--no-sources",
                "--out-dir",
                str(directory / "rebuilt"),
                str(source),
            ],
            cwd=directory,
            check=True,
        )
    for mode in ("client", "server"):
        environment = directory / mode
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(environment)],
            cwd=directory,
            check=True,
        )
        python = environment / "bin" / "python"
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "--constraint",
                str(constraints),
                *[
                    str(wheel)
                    for wheel in sorted(dist.glob("*.whl"))
                    if mode == "server" or not wheel.name.startswith("octomate-")
                ],
            ],
            cwd=directory,
            check=True,
        )
        subprocess.run(
            [str(python), "-I", str(root / ".github/checks/installed.py"), mode],
            cwd=directory,
            check=True,
        )
print(
    "Verified 3 wheels, 3 source distributions, and isolated client/server installations."
)
