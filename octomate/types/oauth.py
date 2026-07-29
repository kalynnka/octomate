from typing import Annotated, Literal

from pydantic import AnyUrl, UrlConstraints

OAuthConnectionStatus = Literal["active", "invalid"]

HttpsUrl = Annotated[AnyUrl, UrlConstraints(allowed_schemes=["https"])]
"""A provider endpoint a user is sent to, which carries an authorization secret in
the page they land on and so is never followed over plaintext."""
