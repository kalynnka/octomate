from typing import Annotated, Literal

from pydantic import AfterValidator, ConfigDict, Field, SecretStr, TypeAdapter

ApiKeyScope = Literal["hooks", "mcp"]


def validate_password_complexity(password: SecretStr) -> SecretStr:
    value = password.get_secret_value()
    if (
        not any(character.islower() for character in value)
        or not any(character.isupper() for character in value)
        or not any(character.isdigit() for character in value)
        or not any(
            not character.isalnum() and not character.isspace() for character in value
        )
    ):
        raise ValueError(
            "Password must include a lowercase letter, an uppercase letter, "
            "a digit, and a symbol"
        )
    return password


NewPassword = Annotated[
    SecretStr,
    Field(
        min_length=11,
        max_length=1024,
        description=(
            "11-1024 characters, including at least one lowercase letter, "
            "uppercase letter, digit, and symbol. Whitespace is not a symbol."
        ),
    ),
    AfterValidator(validate_password_complexity),
]
NewPasswordAdapter = TypeAdapter(
    NewPassword, config=ConfigDict(hide_input_in_errors=True)
)
