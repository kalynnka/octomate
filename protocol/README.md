# Octomate protocol

Shared Pydantic contracts for the Octomate server and CLI:

- Transcript stream messages, discriminated unions, adapters, and protocol version.
- Database backup records exchanged with the server maintenance process.

This package is installed automatically by `octomate` and `octomate-cli`.
It requires Python 3.12 or newer and depends only on Pydantic.
The packages have independent versions and compatible dependency ranges. Stream
compatibility is checked using `STREAM_PROTOCOL`, not package version equality.
Compatible server updates do not require a CLI or protocol package update.

See the [project documentation](https://github.com/kalynnka/octomate).
