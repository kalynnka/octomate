# Add through Trunkline

Use Trunkline's **MCP** panel to install a configured offering or add an MCP
server by URL. Connections are private to your Octomate account. You can add
separate installations for work and personal accounts using distinct namespaces.

## Before you start

Sign in to [Trunkline](../../usage/channels/trunkline.md). For bearer tokens, the
operator must configure `oauth.encryption_key`. For custom OAuth endpoints, both
`oauth.encryption_key` and `oauth.callback_base_uri` are required; see
[OAuth settings](../../installation/settings.md#profile-linking-and-mcp-authorisation).
The callback origin must be reachable from your browser.

Installing saves a connection in Octomate's database for your account. Bearer
tokens and OAuth credentials are encrypted with the server's encryption key.
Once these server settings are in place, adding connections takes effect without
editing `tentacles.yaml` or restarting Octomate.

## Install a configured offering

1. Open **MCP → Tentacles**, find the service and select **Install**. Alternatively,
   select **+ Install MCP** and choose the offering under **Source**.
2. Set **Name** and **Namespace**. The selected tentacle supplies its endpoint
   and authentication settings. For example, use `github_work` to distinguish
   your work account from another GitHub installation.
3. Select **Install MCP**. Trunkline switches to **Installed**. For OAuth,
   the authorization dialog opens automatically; follow the steps below.

If the offering is missing, the operator needs to configure and enable a
[built-in tentacle](builtins.md) or [add a preset](presets.md), then restart
Octomate. An available offering is not yet installed for your account.

## Add a custom endpoint

1. Open **MCP** and select **+ Install MCP**.
2. Leave **Source** set to **Custom Endpoint**.
3. Enter a **Name**, such as `Tracker · Work`, and a **Namespace**, such as
   `tracker_work`. Namespaces must start with a lowercase letter and contain only
   lowercase letters, digits, underscores or hyphens, up to 64 characters.
   They must be unique within your account and cannot be changed after installation.
4. Enter the server's **Endpoint**, such as `https://tools.example.com/mcp`.
   Use its HTTPS MCP endpoint, without URL credentials, a query string or a fragment.
   The endpoint must be reachable from the Octomate server.
5. Choose **Authentication**:

   | Choice | What to provide |
   |---|---|
   | **None** | No credentials; the service accepts unauthenticated requests |
   | **Bearer token** | Paste the token into **Bearer token**; it belongs to this installation |
   | **OAuth** | Complete the provider's browser authorization after installing |

6. Select **Install MCP**. The connection appears under **Installed**. OAuth
   installations open the authorization dialog automatically.

Custom endpoints use remote MCP over Streamable HTTP. This form does not launch
local stdio commands. Custom OAuth requires the service to advertise OAuth
metadata and support dynamic client registration or client metadata documents.
If the provider requires a manually registered app, ask the operator to configure
an [`oauth` tentacle](builtins.md#a-registered-oauth-application) and install that
offering instead.

## Complete OAuth authorization

1. In the authorization dialog, choose a **Method** and select **Continue**.
   Custom OAuth endpoints use **Browser**; configured offerings may also offer
   **Device code**.
2. Complete sign-in and consent in the provider's page. For browser authorization,
   Trunkline opens a new tab; **Open authorization** lets you open it again. For
   device authorization, copy the displayed code and follow **Open authorization**.
3. Return to Trunkline and select **Check status**. Wait for **Ready** before using
   the connection. If you closed the dialog, use **Connect** or **Continue** in
   the installation's row to finish.

## Verify the connection

Check that the connection is enabled under **Installed**, then ask a supported
agent for a small read-only action:

> Use my tracker_work connection to list the tasks assigned to me.

The tool namespace is `personal/tracker_work`. A saved installation alone does
not verify that the endpoint is reachable or that its credentials work; this
request checks access to the service.

To use the connection in another chat channel, [link that profile to the same
Octomate account](../../installation/accounts.md#link-your-channel-profiles).
The [connection usage guide](../../usage/mcp/proxy.md) lists supported agents and explains
how to disable, reconnect or remove an installation.
