"""Octomate's client half: the operator CLI, the native-session hook clients, and the
transcript tail.

Deliberately light: a client machine installs octomate-cli without the server.
Commands that need the server (`serve`, config-derived defaults) import it lazily
and say so plainly when it is absent. Shared stream and deployment contracts live
in octomate_protocol, which depends on neither application.
"""
