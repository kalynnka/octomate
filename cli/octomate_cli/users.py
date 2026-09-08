import asyncio
from importlib.util import find_spec
from typing import Annotated
from urllib.parse import urlencode

import typer
from pydantic import HttpUrl, SecretStr, TypeAdapter, ValidationError

user_typer = typer.Typer(help="Manage local user accounts.", no_args_is_help=True)


def password_error(error: ValidationError) -> typer.BadParameter:
    issue = error.errors(include_input=False, include_url=False)[0]
    context = issue.get("ctx", {})
    if issue["type"] == "too_short":
        message = f"Use at least {context['min_length']} characters."
    elif issue["type"] == "too_long":
        message = f"Use at most {context['max_length']} characters."
    else:
        message = str(context.get("error", issue["msg"]))
    return typer.BadParameter(message, param_hint="--password")


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
    except ValidationError as error:
        raise password_error(error) from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Registered {username}. Sign in through Trunkline.")


@user_typer.command("reset-password")
def reset_password(
    username: Annotated[str, typer.Option(help="Username of the existing account.")],
    password: Annotated[
        str,
        typer.Option(
            help="New password; prompts securely when omitted.",
            prompt=True,
            hide_input=True,
            confirmation_prompt=True,
        ),
    ],
) -> None:
    """Reset an account's password using local administrator access to the database."""
    if find_spec("octomate") is None:
        raise typer.BadParameter("Password reset requires the Octomate server package")

    # Server imports stay here so the standalone client CLI remains usable.
    from octomate.config import OctomateConfig
    from octomate.managers.auth import AuthManager
    from octomate.schemas.base import sqlalchemy_materia

    config = OctomateConfig()
    if config.auth is None:
        raise typer.BadParameter("Configure auth.yaml before resetting passwords")
    manager = AuthManager(config.auth)

    async def reset() -> None:
        with sqlalchemy_materia():
            await manager.set_password(username, SecretStr(password))

    try:
        asyncio.run(reset())
    except ValidationError as error:
        raise password_error(error) from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    typer.echo(f"Password reset for {username}. Browser sessions have been signed out.")
