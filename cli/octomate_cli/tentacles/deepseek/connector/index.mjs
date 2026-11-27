export const name = 'octomate'
export const inject = ['mcpManager', 'mcpAccounts']

export function apply(ctx, config) {
  ctx.mcpManager.register('octomate', {
    transport: 'streamable-http',
    url: config.url,
    headers: { 'X-Octomate-Client': 'deepseek' },
    oauth: { credentialKey: 'mcp-client/octomate', scope: 'mcp' },
    toolLoading: 'eager',
  })
  ctx.effect(() => ctx.mcpAccounts.register({
    server: 'octomate',
    packageName: '@octomate/dsh-connector',
    copy: {
      title: 'Octomate account',
      connect: 'Connect Octomate',
      activation: { title: 'Connect Octomate', body: 'Authorize Octomate to use its tools in DSH.' },
      detail: {
        disconnected: 'Connect your Octomate account to use its tools.',
        waitingBrowser: 'Approve the request in Octomate, then return here.',
        authorizing: 'Finishing authorization with Octomate.',
        connecting: 'Connecting to Octomate.',
        connected: 'Octomate is connected.',
        rejected: 'Octomate no longer accepts this authorization. Connect again.',
        scope: 'Connect again to grant the permissions Octomate requires.',
      },
      signOut: { title: 'Disconnect Octomate?', body: 'Remove the saved DSH authorization for Octomate.' },
      notice: { connected: 'Octomate connected.', signedOut: 'Octomate disconnected.' },
    },
  }), 'Octomate connector account')
}
