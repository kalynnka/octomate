"""SRT policy and ownership checks without installing or launching a runtime."""

import json
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError
from uuid_utils.compat import uuid7

from octomate.config.habitat import SrtHabitatConfig
from octomate.habitat import HabitatError
from octomate.habitat.srt import SrtHabitat
from octomate.schemas.volumes import Volume
from octomate.schemas.volumes.storage import HostDirectory


@pytest.mark.parametrize(
    "domain",
    [
        "example.com",
        "api.example.com:443",
        "example.com:80",
        "*.example.com",
        "*.example.com:8443",
        "localhost",
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]",
        "[::1]:8000",
        "[::ffff:127.0.0.1]:65535",
        "xn--bcher-kva.example",
        "API.example.com",
    ],
)
def test_valid_srt_domains_round_trip(domain: str) -> None:
    config = SrtHabitatConfig(denied_domains=[domain])
    assert config.denied_domains == [domain]
    assert SrtHabitatConfig.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize(
    "domain",
    [
        "",
        "*",
        "*.com",
        "api.*.com",
        "example",
        "https://example.com",
        "example.com/path",
        "example.com?query",
        "user@example.com",
        " example.com",
        "example.com\n",
        "bad_name.example",
        "-bad.example",
        "bad-.example",
        "a..example",
        ".example.com",
        "example.com.",
        "*.127.0.0.1",
        "*.localhost",
        "*.[::1]",
        "999.1.2.3",
        "127.1",
        "0x7f.0.0.1",
        "::1",
        "[not-an-ip]",
        "[::1]suffix",
        "[::1%en0]",
        "example.com:0",
        "example.com:65536",
        "example.com:0443",
        "example.com:-1",
        "example.com:abc",
        "example.com:443.evil.com",
        "[::1]:0",
        "bücher.example",
        "a" * 64 + ".example",
        ".".join(["a" * 63] * 4),
    ],
)
def test_invalid_srt_domains_are_rejected(domain: str) -> None:
    with pytest.raises(ValidationError):
        SrtHabitatConfig(denied_domains=[domain])


def test_srt_domains_reject_non_strings() -> None:
    with pytest.raises(ValidationError):
        SrtHabitatConfig.model_validate({"denied_domains": [123]})


@pytest.fixture
def habitat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SrtHabitat:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "index.js").write_text("")
    executable = runtime / "cli.js"
    executable.write_text("")
    monkeypatch.setattr(
        "octomate.habitat.srt.shutil.which",
        lambda value: "/usr/bin/true" if value == "node" else value,
    )
    return SrtHabitat(
        config=SrtHabitatConfig(executable=str(executable)),
        user_id=uuid7(),
        root=tmp_path / "owner",
    )


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("denied_domains", [[], ["blocked.example:443"]])
async def test_policy_denies_other_storage_and_host_environment(
    habitat: SrtHabitat,
    monkeypatch: pytest.MonkeyPatch,
    denied_domains: list[str],
    read_only: bool,
) -> None:
    habitat.config = habitat.config.model_copy(
        update={"denied_domains": denied_domains}
    )
    habitat.read_only = read_only
    monkeypatch.setenv("OPERATOR_SECRET", "not-for-guests")
    habitat.configure(habitat.config.executable)
    try:
        assert habitat.settings is not None
        policy = json.loads((Path(habitat.settings.name) / "settings.json").read_text())
        assert policy["filesystem"]["denyRead"] == ["/"]
        writable = (
            [habitat.environment()[key] for key in ("HOME", "TMPDIR")]
            if read_only
            else [str(habitat.root)]
        )
        assert policy["filesystem"]["allowWrite"] == [
            {"path": path, "literal": True} for path in writable
        ]
        assert policy["network"]["allowLocalBinding"] is False
        assert policy["network"]["allowedDomains"] == []
        assert policy["network"]["deniedDomains"] == denied_domains
        assert policy["network"]["strictAllowlist"] is False
        assert "OPERATOR_SECRET" not in habitat.environment()
        assert Path(habitat.environment()["HOME"]).is_relative_to(habitat.root)
        assert Path(habitat.environment()["TMPDIR"]).is_relative_to(habitat.root)
        settings = Path(habitat.settings.name)
        assert not settings.is_relative_to(habitat.root)
    finally:
        await habitat.stop()
    assert not await anyio.Path(settings).exists()


def test_volume_owner_and_resolved_path_are_checked(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    habitat.root.mkdir()
    volume = Volume(owner_id=habitat.user_id, storage=HostDirectory(habitat.root))
    assert str(habitat.path(volume)) == str(habitat.root)
    with pytest.raises(HabitatError, match="owner"):
        habitat.path(volume.model_copy(update={"owner_id": uuid7()}))
    link = habitat.root / "escape"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(HabitatError, match="owner"):
        habitat.path(Volume(owner_id=habitat.user_id, storage=HostDirectory(link)))


async def test_prepare_checks_platform_and_executable(
    habitat: SrtHabitat, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("octomate.habitat.srt.platform.system", lambda: "Linux")
    with pytest.raises(HabitatError, match="macOS only"):
        await habitat.prepare()
    monkeypatch.setattr("octomate.habitat.srt.platform.system", lambda: "Darwin")
    monkeypatch.setattr("octomate.habitat.srt.shutil.which", lambda _: None)
    with pytest.raises(HabitatError, match="Install SRT"):
        await habitat.prepare()
    assert habitat.settings is None
    monkeypatch.setattr(
        "octomate.habitat.srt.shutil.which",
        lambda value: "/usr/bin/true" if value == "node" else value,
    )
    try:
        await habitat.prepare()
        assert habitat.settings is not None
    finally:
        await habitat.stop()


async def test_resident_rejects_unprepared_and_external_cwd(
    habitat: SrtHabitat, tmp_path: Path
) -> None:
    with pytest.raises(HabitatError, match="Prepare"):
        async with habitat.resident("true", workdir=str(tmp_path)):
            pytest.fail("Unprepared habitat was entered")
    habitat.configure(habitat.config.executable)
    try:
        with pytest.raises(HabitatError, match="working directory"):
            async with habitat.resident("true", workdir=str(tmp_path)):
                pytest.fail("External directory was accepted")
    finally:
        await habitat.stop()
