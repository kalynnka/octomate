"""Octomate's client half: the operator CLI, the native-session hook clients, and the
transcript tail.

Deliberately light: a client machine installs octomate-cli alone, and nothing in this
package imports the octomate server package — commands that need the server
(`serve`, config-derived defaults) import it lazily and say so plainly when it is
absent. The one module the server imports from here is `octomate_cli.stream`, the
wire protocol — cheap to reach, since this file deliberately imports nothing.
"""
