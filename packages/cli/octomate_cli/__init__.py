"""Octomate's client half: the operator CLI, the native-session hook clients, and the
transcript tail.

Deliberately light: a client machine installs octomate-cli alone, and nothing in this
package imports the octomate server package — commands that need the server
(`serve`, config-derived defaults) import it lazily and say so plainly when it is
absent. The distribution also ships `octomate_stream`, the wire protocol, as its own
top-level module: the server imports that from it, and nothing else.
"""
