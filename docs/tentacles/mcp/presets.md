# Add presets

The CLI currently ships **GitHub** as its only provider preset. The same preset
is available in the interactive setup wizard. It has two endpoint options:

| Preset | CLI option | Endpoint | Tools offered |
|---|---|---|---|
| GitHub | Default | `https://api.githubcopilot.com/mcp/` | GitHub's default toolset |
| GitHub, read-only | `--read-only` | `https://api.githubcopilot.com/mcp/readonly` | Read-only tools from that toolset |

The [packaged preset](https://github.com/kalynnka/octomate/blob/main/cli/octomate_cli/presets/github.yaml)
supplies OAuth endpoints, scopes and both device-code and browser authorisation.
GitHub documents the endpoint options in its
[remote MCP guide](https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md).
Other providers can be configured with the [built-in tentacles](builtins.md).

## Add the GitHub preset

### 1. Register an OAuth app

Follow GitHub's [OAuth app setup](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app).
Enable **Device Flow**, note the client id, and create a client secret. Register
the browser callback as `<your-octomate-origin>/oauth/github/callback`.
If you choose a different tentacle id below, replace `github` in that path too.

Use the same origin as `oauth.callback_base_uri`. Your browser must be able to
reach it; keep Octomate behind your [private connection](../../installation/networking.md).
The OAuth app belongs to the deployment; each user authorises their own GitHub
account later.

### 2. Write it to the config home

Run on the server host with the CLI installed:

```sh
export OCTOMATE_HOME="/absolute/path/to/your/config"
octomate mcp preset github --client-id '<your-oauth-app-client-id>'
```

This adds a `github` entry to `$OCTOMATE_HOME/tentacles.yaml`. If you omit
`--client-id`, the CLI prompts for it. Existing settings are retained, but the
file is rewritten as YAML, so comments and formatting are not preserved. An
existing tentacle with the same id is refused rather than replaced.

To create a separately named read-only offering instead:

```sh
octomate mcp preset github --client-id '<your-oauth-app-client-id>' \
  --name github_readonly --read-only
```

Its secret variable becomes `OCTOMATE__TENTACLES__GITHUB_READONLY__CLIENT_SECRET`
and its callback path is `/oauth/github_readonly/callback`. Keep the chosen id
stable after users install it.

To review the generated configuration before merging it into your config home,
add `--output /path/to/review/tentacles.yaml`. Octomate only loads it after you
merge the entry into the active `tentacles.yaml`. Presets expand into ordinary
tentacle settings; there is no `preset:` setting to add to YAML.

!!! note "Read-only tools, unchanged OAuth scopes"
    `--read-only` selects GitHub's read-only MCP endpoint. It does not narrow the
    OAuth scopes requested by the preset, which still include write access.
    Review the app's consent screen before authorising it.

### 3. Supply the secret and OAuth settings

Set `client_secret` in the generated `github` YAML block, or supply
`OCTOMATE__TENTACLES__GITHUB__CLIENT_SECRET` in the server environment. `.env` is
optional; if using it, remove any matching YAML placeholder.

Configure `oauth.callback_base_uri` and `oauth.encryption_key` as described in
[Server settings](../../installation/settings.md#profile-linking-and-mcp-authorisation).
Both are needed for the preset's browser flow; the encryption key also protects
device-flow credentials. Keep an existing encryption key unchanged. The settings
guide also covers key format and preservation when upgrading older installations.

??? details "Equivalent YAML for the standard GitHub preset"

    You can add this entry under your existing `tentacles:` key instead of using
    the CLI. Replace the client id and provide the client secret through the
    environment or an additional `client_secret` field.

    ```yaml
    tentacles:
      github:
        type: oauth
        url: https://api.githubcopilot.com/mcp/
        client_id: your-oauth-app-client-id
        scopes:
          - repo
          - read:org
          - read:user
          - user:email
          - write:packages
          - project
          - gist
          - notifications
          - workflow
        scope_separator: ","
        invalid_credentials_errors: [invalid_grant, invalid_client, bad_refresh_token, incorrect_client_credentials]
        flows:
          - type: device
            device_authorization_endpoint: https://github.com/login/device/code
            token_endpoint: https://github.com/login/oauth/access_token
          - type: authorization_code
            authorization_endpoint: https://github.com/login/oauth/authorize
            token_endpoint: https://github.com/login/oauth/access_token
            token_endpoint_auth_method: client_secret_post
    ```

### 4. Enable the offering

The generated entry is enabled by default. If you previously set `enabled: false`,
change it to `true`. [Check the configuration](../../installation/configuration.md#validate-without-starting)
and restart Octomate. It is now available for users to install; generating the
configuration does not connect anyone's GitHub account.

## Connect it to your account

Choose either route below. Use the configured offering so the installation picks
up its app and endpoint settings. A personal namespace such as `github_work`
identifies your installation; it does not change the server's tentacle id or
registered callback URL.

=== "Trunkline"

    Follow [Install a configured offering](trunkline.md#install-a-configured-offering),
    selecting `github` or the custom id chosen by the operator. Choose **Device
    code** or **Browser** when connecting your account.

=== "Conversation"

    Use a channel that can deliver private authorisation messages, with your
    [profile linked to your Octomate account](../../installation/accounts.md#link-your-channel-profiles).
    Choose Inkling, Claude Code or Codex with Octomate tools available.

    Ask:

    > Show the available MCP offerings. Install the GitHub offering for my account
    > as github_work, then help me connect it using device authorisation.

    The agent installs your connection and sends the authorisation link and code
    privately. Open the link, enter the code and approve access on GitHub. Then
    return to the conversation:

    > I've completed GitHub authorisation. Check that github_work is connected.

    You can request browser authorisation instead of device authorisation. You
    complete the provider's sign-in yourself; do not paste credentials into chat.
    If the conversation cannot receive private authorisation messages, use the
    Trunkline MCP panel. A native MCP session alone has no channel for delivering
    that link.

Verify from either route with a small read-only request:

> Use my github_work connection to list the open issues in the repository I specify.

Your installation appears as `personal/github_work` in tool results. Other users
repeat the connection process for their own accounts. If an offering is missing,
ask the operator to check its id, enabled state and server restart. If it is
installed but not ready, finish or restart its authorisation before retrying.
