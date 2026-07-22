from __future__ import annotations

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_DB_URL = "sqlite+aiosqlite:///.octomate/octomate.db"


class DatabaseSettings(BaseSettings):
    """Where the database lives — one resolution shared by the app engine
    (octomate/database.py) and alembic (migrations/env.py). Sources, strongest
    first: the OCTOMATE_DB_URL environment variable, `db_url:` under
    `octomate:` in octomate.yaml, `.env`, the SQLite default.

    Deliberately not `OctomateConfig`: instantiating that validates the whole
    app config (agents, channels, secrets), and migrations must not require a
    fully valid deployment just to find the database.
    """

    model_config = SettingsConfigDict(
        env_prefix="OCTOMATE_",
        env_file=".env",
        yaml_file=("octomate.default.yaml", "octomate.yaml"),
        yaml_config_section="octomate",
        extra="ignore",
    )

    db_url: str = DEFAULT_DB_URL

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Same source ordering as OctomateConfig: env beats yaml beats .env.
        return (
            init_settings,
            env_settings,
            YamlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


# The project-wide instance, resolved once at import — every consumer (the app
# engine, alembic) shares this object rather than re-reading the sources.
database_settings = DatabaseSettings()
