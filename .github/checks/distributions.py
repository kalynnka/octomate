"""Rebuild source distributions and exercise wheels in isolated installations."""

import subprocess
import sys
import tempfile
import tomllib
from email import message_from_bytes
from pathlib import Path
from zipfile import ZipFile

from packaging.requirements import Requirement

root = Path(__file__).resolve().parents[2]
dist = root / "dist"
projects = [
    tomllib.loads((root / path / "pyproject.toml").read_text())["project"]
    for path in (".", "cli", "protocol")
]
versions = {project["name"]: project["version"] for project in projects}
assert len(list(dist.glob("*.whl"))) == 3
assert len(list(dist.glob("*.tar.gz"))) == 3
for wheel in dist.glob("*.whl"):
    with ZipFile(wheel) as archive:
        metadata_file = next(
            name for name in archive.namelist() if name.endswith("/METADATA")
        )
        metadata = message_from_bytes(archive.read(metadata_file))
        assert metadata["Version"] == versions[metadata["Name"]], wheel
        dependencies = {
            dependency.name: dependency
            for value in metadata.get_all("Requires-Dist", [])
            if (dependency := Requirement(value)).name in versions
        }
        expected = {
            "octomate": {"octomate-cli", "octomate-protocol"},
            "octomate-cli": {"octomate-protocol"},
            "octomate-protocol": set(),
        }
        assert dependencies.keys() == expected[metadata["Name"]], wheel
        for name, dependency in dependencies.items():
            assert versions[name] in dependency.specifier, (wheel, dependency)
            assert all(spec.operator != "==" for spec in dependency.specifier)
        if metadata["Name"] == "octomate":
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
