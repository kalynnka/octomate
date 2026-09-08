import asyncio
from importlib.util import find_spec
from typing import Annotated
from urllib.parse import urlencode

import typer
from pydantic import HttpUrl, SecretStr, TypeAdapter

user_typer = typer.Typer(help="Manage local user accounts.", no_args_is_help=True)


def invite(
    url: Annotated[
        str | None,
        typer.Option(help="Print a registration link for this URL instead of a code."),
    ] = None,
) -> None:
    """Issue an anonymous, single-use registration invitation."""
    if find_spec("octomate") is None:
        raise typer.BadParameter("Invitations require the Octomate server package")

    # Server imports stay here so the standalone client CLI remains usable.
    from octomate.config import OctomateConfig
    from octomate.managers.auth import AuthManager
    from octomate.schemas.base import sqlalchemy_materia

    base_url = TypeAdapter(HttpUrl).validate_python(url) if url is not None else None
    config = OctomateConfig()
    if config.auth is None:
        raise typer.BadParameter("Configure auth.yaml before issuing invitations")
    manager = AuthManager(config.auth)

    async def issue() -> str:
        with sqlalchemy_materia():
            token = await manager.invite()
        return token.get_secret_value()

    token = asyncio.run(issue())
    if base_url is None:
        typer.echo(token)
    else:
        fragment = urlencode({"invitation": token})
        typer.echo(f"{str(base_url).rstrip('/')}/#{fragment}")


@user_typer.command("create")
def create(
    username: Annotated[str, typer.Option(help="Username for the new account.")],
    password: Annotated[str, typer.Option(help="Password for the new account.")],
    invitationcode: Annotated[
        str, typer.Option(help="An unused registration invitation code.")
    ],
    name: Annotated[
        str | None, typer.Option(help="Display name; defaults to the username.")
    ] = None,
) -> None:
    """Register a user with an invitation using the server's config and database."""
    if find_spec("octomate") is None:
        raise typer.BadParameter("Registration requires the Octomate server package")

    # Server imports stay here so the standalone client CLI remains usable.
    from octomate.config import OctomateConfig
    from octomate.managers.auth import AuthManager
    from octomate.schemas.base import sqlalchemy_materia

    config = OctomateConfig()
    if config.auth is None:
        raise typer.BadParameter("Configure auth.yaml before registering users")
    manager = AuthManager(config.auth)

    async def register() -> None:
        with sqlalchemy_materia():
            await manager.register(
                username,
                SecretStr(password),
                SecretStr(invitationcode),
                name=username if name is None else name,
            )

    try:
        asyncio.run(register())
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Registered {username}. Sign in through Trunkline.")
