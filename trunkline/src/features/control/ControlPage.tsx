import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useConsole } from '@/state/console'
import { useAgents, useMcpServers, useMcpTentacles, useProfile } from '@/lib/api/hooks'
import { refusalText } from '@/lib/api/auth'
import { enableMcp, uninstallMcp } from '@/lib/api/client'
import { channelMeta } from '@/lib/api/live'
import { queryClient } from '@/lib/queryClient'
import type { ControlSection } from '@/state/console'
import { Button } from '@/components/Button'
import { BracketTabs } from '@/components/BracketTabs'
import { Table } from '@/components/Table'
import type { TableColumn } from '@/components/Table'
import {
  display,
  ellipsis,
  label,
  microSection,
  mono,
  statusNote,
} from '@/components/text'
import { AccountPanel, ApiKeysPanel } from '@/features/auth/AccountPanel'
import { Refusal } from '@/features/auth/parts'
import type {
  ApiAgentInfo,
  ApiMcpServerSummary,
  ApiMcp,
  ApiMcpTentacle,
  ApiUserProfile,
} from '@/lib/api/events'
import type { EffortStep } from '@/lib/api/types'
import { SettingsPanel } from './SettingsPanel'
import { McpInstallDialog } from './McpInstallDialog'
import { McpAuthorizationDialog } from './McpAuthorizationDialog'
import { controlHints } from './sections'

const pages: Record<Exclude<ControlSection, ''>, { title: string; desc: string }> = {
  agents: {
    title: 'Agents',
    desc: 'Routed agents, the models each answers on, and the effort each route takes.',
  },
  mcp: {
    title: 'MCP',
    desc: 'Your installed MCPs and the configured tentacles you can connect to.',
  },
  profile: {
    title: 'Profile',
    desc: 'Your account information and password.',
  },
  channels: { title: 'Channels', desc: 'Your identities on connected channel tentacles.' },
  keys: { title: 'API Keys', desc: 'Manage access for clients and native session hooks.' },
  dash: { title: 'Dashboard', desc: 'Agents, routes and your connected services at a glance.' },
  settings: { title: 'Settings', desc: 'Choose how Trunkline looks on this browser.' },
}

const order: Exclude<ControlSection, ''>[] = ['dash', 'agents', 'mcp', 'profile', 'channels', 'keys', 'settings']

const effortScale: EffortStep[] = ['minimal', 'low', 'medium', 'high', 'xhigh']

function Effort({ efforts }: { efforts: EffortStep[] }) {
  const description = efforts.length
    ? `Supported effort: ${efforts.join(', ')}`
    : 'No configurable effort'
  return (
    <span
      className="trk-effort"
      role="img"
      tabIndex={0}
      title={description}
      aria-label={description}
    >
      {effortScale.map((step) => {
        const name = step === 'xhigh' ? 'Extra high' : step.charAt(0).toUpperCase() + step.slice(1)
        const supported = efforts.includes(step)
        return (
          <span
            key={step}
            title={`${name}: ${supported ? 'supported' : 'not supported'}`}
            data-supported={supported}
          />
        )
      })}
    </span>
  )
}

function DotCell({ color, children }: { color: string; children: ReactNode }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 600,
      color: `color-mix(in srgb, ${color} 50%, var(--fg-1))`,
    }}>
      <i style={{ width: 6, height: 6, background: color, display: 'block', flexShrink: 0 }} />
      {children}
    </span>
  )
}

/** Stacked cell — one line per model, so the models and effort columns line up
 *  row for row against the agent beside them. */
function Stacked({ children }: { children: ReactNode }) {
  return <span style={{ display: 'flex', flexDirection: 'column', gap: 4, lineHeight: '20px' }}>{children}</span>
}

function agentState(agent: ApiAgentInfo): { text: string; color: string } {
  if (agent.driven_sessions > 0) {
    return { text: `driving ${agent.driven_sessions}`, color: 'var(--color-accent)' }
  }
  if (agent.native_sessions > 0) {
    return { text: `reading ${agent.native_sessions}`, color: 'var(--color-teal)' }
  }
  return { text: 'idle', color: 'var(--fg-3)' }
}

const agentColumns: TableColumn<ApiAgentInfo>[] = [
  {
    key: 'id',
    label: 'Agent',
    mono: true,
    width: '25%',
    render: (a) => {
      const name = a.id.charAt(0).toUpperCase() + a.id.slice(1)
      const { text, color } = agentState(a)
      const detail = `${a.description}\nGateway: ${a.gateway ? 'on' : 'off'}\nActivity: ${text}`
      return (
        <span title={detail} tabIndex={0} aria-label={`${name}. ${detail}`} className="trk-agent-name">
          <i style={{ background: color }} />
          <span style={{ ...ellipsis, color: 'var(--fg-1)', fontWeight: 700 }}>{name}</span>
        </span>
      )
    },
  },
  {
    key: 'models',
    label: 'Models',
    mono: true,
    render: (a) => (
      <Stacked>
        {a.routes.map((m) => (
          <span
            key={m.model}
            style={{
              display: 'flex', alignItems: 'center', gap: 4, minWidth: 0,
              color: m.model === a.default_model ? 'var(--fg-1)' : 'var(--fg-2)',
              fontWeight: m.model === a.default_model ? 700 : 500,
            }}
          >
            <span style={ellipsis}>{m.model}</span>
            {m.model === a.default_model && (
              <span
                style={{ color: 'var(--color-accent)', flexShrink: 0 }}
                role="img"
                aria-label="Default model"
              >
                ★
              </span>
            )}
          </span>
        ))}
      </Stacked>
    ),
  },
  {
    key: 'effort',
    label: 'Effort',
    width: 86,
    render: (a) => (
      <Stacked>
        {a.routes.map((m) => (
          <Effort key={m.model} efforts={m.claim.efforts} />
        ))}
      </Stacked>
    ),
  },
]

const serverColumns: TableColumn<ApiMcp>[] = [
  {
    key: 'name',
    label: 'Name',
    mono: true,
    width: '16%',
    render: (m) => <span style={{ color: 'var(--fg-1)', fontWeight: 700 }}>{m.name}</span>,
  },
  {
    key: 'namespace',
    label: 'Namespace',
    mono: true,
    width: '20%',
    render: (m) => <span style={{ color: 'color-mix(in srgb, var(--color-accent) 60%, var(--fg-1))', fontWeight: 600 }}>{m.namespace}</span>,
  },
  {
    key: 'url',
    label: 'Endpoint',
    mono: true,
    width: '25%',
    render: (m) => <span title={m.url} style={{ ...ellipsis, display: 'block', maxWidth: 240, fontWeight: 400 }}>{m.url}</span>,
  },
  { key: 'auth', label: 'Auth', mono: true, width: '8%', render: (m) => m.auth_kind },
  {
    key: 'source',
    label: 'Tentacle',
    mono: true,
    width: '14%',
    render: (m) => m.tentacle_id ?? '—',
  },
]

const tentacleColumns: TableColumn<ApiMcpTentacle>[] = [
  {
    key: 'name',
    label: 'Name',
    mono: true,
    width: '22%',
    render: (t) => <span style={{ color: 'var(--fg-1)', fontWeight: 700 }}>{t.name}</span>,
  },
  { key: 'id', label: 'ID', mono: true, render: (t) => t.id },
  {
    key: 'url',
    label: 'Endpoint',
    mono: true,
    render: (t) => <span title={t.url} style={{ ...ellipsis, display: 'block', maxWidth: 280, fontWeight: 400 }}>{t.url}</span>,
  },
  { key: 'auth', label: 'Auth', mono: true, render: (t) => t.auth_kind },
]

function McpConnection({ mcp, grant, loading, onConnect }: {
  mcp: ApiMcp
  grant?: ApiMcpServerSummary
  loading: boolean
  onConnect: () => void
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const status = grant?.oauth?.status
  const pending = status === 'pending_browser' || status === 'pending_device'
  const ready = mcp.enabled && (mcp.auth_kind !== 'oauth' || status === 'active')
  const unavailable = mcp.auth_kind === 'oauth' && !grant?.oauth
  const text = !mcp.enabled ? 'Disabled' : ready ? 'Ready'
    : unavailable ? loading ? 'Loading…' : 'Unavailable' : 'Pending'
  const color = ready ? 'var(--color-teal)' : mcp.enabled && pending ? 'var(--color-accent)' : 'var(--fg-3)'

  const enable = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await enableMcp(mcp.id)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['mcp-servers'] }),
        queryClient.invalidateQueries({ queryKey: ['profile'] }),
      ])
    } catch (caught) {
      setError(refusalText(caught) ?? 'The MCP could not be enabled.')
    } finally { setBusy(false) }
  }

  return (
    <div className="trk-mcp-connection">
      {!mcp.enabled || pending ? (
        <button
          type="button"
          className="trk-mcp-status hov-wash"
          disabled={busy}
          title={mcp.enabled ? 'Continue authorization' : 'Enable MCP'}
          onClick={mcp.enabled ? onConnect : () => void enable()}
        >
          <DotCell color={color}>{busy ? 'Enabling…' : text}</DotCell>
        </button>
      ) : ready || unavailable ? (
        <DotCell color={color}>{text}</DotCell>
      ) : (
        <Button style={{
          color: 'color-mix(in srgb, var(--color-teal) 60%, var(--fg-1))',
          borderColor: 'var(--color-teal)',
          background: 'color-mix(in srgb, var(--color-teal) 10%, transparent)',
        }} onClick={onConnect}>Connect</Button>
      )}
      {error && <div role="alert"><Refusal>{error}</Refusal></div>}
    </div>
  )
}

function McpRemoval({ mcp, onRemoved }: { mcp: ApiMcp; onRemoved: () => void }) {
  const [arming, setArming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const remove = async () => {
    if (busy) return
    setError(null)
    if (!arming) {
      setArming(true)
      return
    }
    setBusy(true)
    try {
      await uninstallMcp(mcp.id)
      onRemoved()
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['mcp-servers'] }),
        queryClient.invalidateQueries({ queryKey: ['profile'] }),
      ])
    } catch (caught) {
      setError(refusalText(caught) ?? 'The MCP could not be removed.')
    } finally {
      setBusy(false)
      setArming(false)
    }
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 6 }}>
        <Button
          disabled={busy}
          title={`Remove ${mcp.name} (${mcp.namespace})`}
          style={{
            color: 'color-mix(in srgb, var(--color-red) 60%, var(--fg-1))',
            borderColor: 'var(--color-red)',
            background: 'color-mix(in srgb, var(--color-red) 10%, transparent)',
          }}
          onClick={() => void remove()}
        >{busy ? 'Removing…' : arming ? 'Confirm remove' : <span role="img" aria-label={`Remove ${mcp.name} (${mcp.namespace})`}>−</span>}</Button>
        {arming && <Button variant="ghost" disabled={busy} onClick={() => setArming(false)}>Cancel</Button>}
      </div>
      {error && <div role="alert"><Refusal>{error}</Refusal></div>}
    </div>
  )
}

function ChannelProfilePanel({ profile, onClose }: { profile: ApiUserProfile; onClose: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    heading.current?.focus()
  }, [profile.id])

  return (
    <aside
      id="trk-channel-profile"
      className="trk-control-card trk-profile-detail"
      aria-labelledby="trk-channel-profile-title"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation()
          onClose()
        }
      }}
    >
      <header className="trk-profile-detail-header">
        <h2 id="trk-channel-profile-title" ref={heading} tabIndex={-1} style={label(11)}>
          Channel profile
        </h2>
        <Button variant="ghost" onClick={onClose} style={{ padding: '5px 8px', fontSize: 9 }}>
          Close
        </Button>
      </header>
      <div className="trk-control-scroll">
        <dl className="trk-profile-info">
          {[
            ['Name', profile.name],
            ['Nickname', profile.nickname],
            ['Title', profile.title],
            ['Gender', profile.gender],
            ['Age', profile.age],
            ['Channel', channelMeta(profile.channel_tentacle_id).label],
            ['Account ID', profile.channel_user_id],
            ['Profile ID', profile.id],
            ['User ID', profile.user_id],
          ].map(([name, value]) => (
            <div key={name}>
              <dt style={{ ...label(9), color: 'var(--fg-3)' }}>{name}</dt>
              <dd style={{ ...mono(12), color: 'var(--fg-1)' }}>
                {value === null || value === '' ? '—' : value}
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </aside>
  )
}

/** The comp's masthead: the section's number and title over an accent hairline, its
 *  standing on the right, and the rules that close the block. */
function Masthead({
  num,
  title,
  side,
  menu,
  desc,
  meta,
}: {
  num: string
  title: string
  side: string
  menu: string
  desc: string
  meta: string
}) {
  return (
    <>
      <div style={{ display: 'flex', alignItems: 'flex-end', flexWrap: 'wrap', gap: '10px 22px', marginTop: 14 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: 22, minWidth: 0 }}>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 1, flexShrink: 0 }}>
              <span
                style={{
                  ...display(38),
                  lineHeight: 0.86,
                  letterSpacing: '-.03em',
                  color: 'var(--fg-1)',
                }}
              >
                {num}
              </span>
              <span style={{ ...display(38), lineHeight: 0.86, color: 'var(--color-accent)' }}>
                :
              </span>
            </div>
            <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, minWidth: 0 }}>
              <span
                style={{ ...label(7.5), color: 'var(--fg-3)', paddingTop: 3, flexShrink: 0 }}
              >
                Title
              </span>
              <span
                style={{
                  ...display(26),
                  lineHeight: 1,
                  letterSpacing: '-.02em',
                  color: 'var(--fg-1)',
                }}
              >
                {title}
              </span>
            </div>
          </div>
          <i
            style={{
              display: 'block',
              height: 2,
              width: 'calc(100% + 34px)',
              background: 'color-mix(in srgb, var(--color-accent) 70%, white)',
            }}
          />
        </div>
        <span style={{ flex: 1 }} />
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'flex-end',
            marginBottom: 9,
            flexShrink: 0,
          }}
        >
          <span style={{ ...label(7.5), color: 'var(--fg-3)' }}>{side}</span>
          <span
            style={{
              ...display(16),
              lineHeight: 1.15,
              letterSpacing: '-.02em',
              color: 'var(--fg-1)',
            }}
          >
            {menu}
          </span>
        </div>
      </div>
      <div style={{ margin: '1px 0 7px' }}>
        <i style={{ display: 'block', height: 2, width: '100%', background: 'var(--color-ink)' }} />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 9.5, color: 'var(--fg-3)' }}>{desc}</span>
        <span style={{ flex: 1 }} />
        <span style={{ ...label(9, '.12em'), color: 'var(--fg-2)' }}>{meta}</span>
      </div>
    </>
  )
}

function Awaiting({ note }: { note: string }) {
  return (
    <div style={{ padding: '26px 16px', textAlign: 'center', ...statusNote, color: 'var(--fg-3)' }}>
      {note}
    </div>
  )
}

type McpView = 'installed' | 'tentacles'

/** Full-column Control page shown in the main column when a section is picked. */
export function ControlPage() {
  const mgmtSec = useConsole((s) => s.mgmtSec)
  const { goChat } = useConsole((s) => s.actions)
  const [mcpView, setMcpView] = useState<McpView>('installed')
  const [installSource, setInstallSource] = useState<ApiMcpTentacle | 'custom' | null>(null)
  const [authorization, setAuthorization] = useState<ApiMcp | null>(null)
  const [installed, setInstalled] = useState<ApiMcp | null>(null)
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null)
  const profileTrigger = useRef<HTMLButtonElement>(null)
  const agentsQuery = useAgents()
  const serversQuery = useMcpServers()
  const tentaclesQuery = useMcpTentacles()
  const profileQuery = useProfile()
  const agents = agentsQuery.data
  const servers = serversQuery.data
  const tentacles = tentaclesQuery.data
  const profile = profileQuery.data
  const selectedProfile = mgmtSec === 'channels'
    ? profile?.profiles.find((p) => p.id === selectedProfileId)
    : undefined

  if (!mgmtSec) return null
  const page = pages[mgmtSec]
  const num = `0${order.indexOf(mgmtSec) + 1}`
  const routeCount = agents?.reduce((n, a) => n + a.routes.length, 0)
  const enabledCount = servers?.filter((s) => s.enabled).length
  const grants = new Map(profile?.mcps.map((mcp) => [mcp.id, mcp]))
  const profileColumns: TableColumn<ApiUserProfile>[] = [
    {
      key: 'channel',
      label: 'Channel',
      mono: true,
      width: '34%',
      render: (p) => {
        const channel = channelMeta(p.channel_tentacle_id)
        return (
          <span style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--fg-1)', fontWeight: 700 }}>
            <i aria-hidden="true" style={{ width: 5, height: 5, flexShrink: 0, background: channel.brand }} />
            {channel.label}
          </span>
        )
      },
    },
    {
      key: 'profile',
      label: 'Profile',
      render: (p) => (
        <button
          type="button"
          className="trk-profile-select"
          aria-label={`View ${p.name || p.channel_user_id}'s ${channelMeta(p.channel_tentacle_id).label} profile`}
          aria-expanded={selectedProfile?.id === p.id}
          aria-controls={selectedProfile?.id === p.id ? 'trk-channel-profile' : undefined}
          onClick={(event) => {
            profileTrigger.current = event.currentTarget
            setSelectedProfileId(p.id)
          }}
        >
          <span>
            <span style={{ ...mono(12, 700), display: 'block' }}>{p.name || p.channel_user_id}</span>
            {p.nickname && (
              <span style={{ ...mono(10), color: 'var(--fg-3)' }}>{p.nickname}</span>
            )}
          </span>
          <span aria-hidden="true">→</span>
        </button>
      ),
    },
  ]
  const installedColumns: TableColumn<ApiMcp>[] = [
    ...serverColumns,
    {
      key: 'connection',
      label: 'Connection',
      mono: true,
      width: '11%',
      render: (mcp) => <McpConnection mcp={mcp} grant={grants.get(mcp.id)} loading={profileQuery.isPending} onConnect={() => setAuthorization(mcp)} />,
    },
    {
      key: 'actions',
      label: '',
      ariaLabel: 'Actions',
      width: '6%',
      render: (mcp) => <McpRemoval mcp={mcp} onRemoved={() => {
        if (installed?.id === mcp.id) setInstalled(null)
      }} />,
    },
  ]
  const presetColumns: TableColumn<ApiMcpTentacle>[] = [
    ...tentacleColumns,
    {
      key: 'installation',
      label: 'Status',
      mono: true,
      render: (tentacle) => {
        const mcps = servers?.filter((mcp) => mcp.tentacle_id === tentacle.id)
        return mcps === undefined ? (
          <DotCell color="var(--fg-3)">{serversQuery.isPending ? 'Loading…' : 'Unavailable'}</DotCell>
        ) : mcps.length > 0 ? (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
            {mcps.map((mcp) => (
              <div key={mcp.id} style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                {mcps.length > 1 && <span title={mcp.namespace} style={{ ...ellipsis, maxWidth: 160 }}>{mcp.namespace}</span>}
                <McpConnection mcp={mcp} grant={grants.get(mcp.id)} loading={profileQuery.isPending} onConnect={() => setAuthorization(mcp)} />
              </div>
            ))}
          </div>
        ) : (
          <Button
            style={{
              color: 'color-mix(in srgb, var(--color-accent) 60%, var(--fg-1))',
              borderColor: 'var(--color-accent)',
              background: 'color-mix(in srgb, var(--color-accent) 10%, transparent)',
            }}
            title={`Install ${tentacle.name}`}
            onClick={() => {
              setInstalled(null)
              setInstallSource(tentacle)
            }}
          >Install</Button>
        )
      },
    },
  ]
  const count =
    mgmtSec === 'agents' ? agents?.length
      : mgmtSec === 'mcp' ? (mcpView === 'installed' ? servers?.length : tentacles?.length)
        : mgmtSec === 'channels' ? profile?.profiles.length : undefined
  const menu =
    mgmtSec === 'agents' ? `${routeCount ?? '—'} Routes`
      : mgmtSec === 'mcp' ? `${enabledCount ?? '—'} Enabled`
        : mgmtSec === 'profile' && profile ? `@${profile.user.username}`
          : mgmtSec === 'settings' ? 'This browser' : ''

  return (
    <div className="trk-control-page">
      <div id="trk-page" className="lt-entry trk-control-card">
        <header className="trk-control-header">
          <Button onClick={goChat} style={{ padding: '4px 9px', fontSize: 9 }}>
            ← Back to chat
          </Button>
          <Masthead
            num={num}
            title={page.title}
            side={`Control / ${page.title}`}
            menu={menu}
            desc={page.desc}
            meta={controlHints[mgmtSec]}
          />
        </header>

        <div className="trk-control-content" key={mgmtSec}>
          {mgmtSec === 'agents' && (agents ? (
            <Table
              className="trk-quiet-scroll trk-agent-table"
              columns={agentColumns}
              rows={agents}
              rowKey={(a) => a.id}
              dense
              empty="No agents are registered."
            />
          ) : (
            <Awaiting note={agentsQuery.isError ? 'Could not load agents.' : 'Loading agents…'} />
          ))}

          {mgmtSec === 'mcp' && (
            <>
              <div style={{ maxWidth: 360, flexShrink: 0, marginBottom: 16 }}>
                <BracketTabs
                  tabs={[
                    { id: 'installed', label: 'Installed' },
                    { id: 'tentacles', label: 'Tentacles' },
                  ]}
                  current={mcpView}
                  onPick={setMcpView}
                />
              </div>
              <p className="trk-control-note">
                {mcpView === 'installed'
                  ? 'These MCPs belong to you. Each namespace identifies a separate installation, including multiple workspaces from the same service.'
                  : 'Configured MCP templates. Install a preset for your account.'}
              </p>
              {installed && (
                <p className="trk-control-note" role="status">
                  Installed {installed.name} as <strong>{installed.namespace}</strong>.
                </p>
              )}
              {installSource && (
                <McpInstallDialog
                  preset={installSource === 'custom' ? undefined : installSource}
                  onClose={() => setInstallSource(null)}
                  onInstalled={(mcp) => {
                    setInstalled(mcp)
                    setMcpView('installed')
                    setInstallSource(null)
                    if (mcp.auth_kind === 'oauth') setAuthorization(mcp)
                  }}
                />
              )}
              {authorization && <McpAuthorizationDialog key={authorization.id} mcp={authorization} onClose={() => setAuthorization(null)} />}
              {mcpView === 'installed' && (
                <button type="button" className="trk-create-button hov-accent-border-wash" onClick={() => {
                  setInstalled(null)
                  setInstallSource('custom')
                }}>+ Install MCP</button>
              )}
              {mcpView === 'installed' ? (servers ? (
                <Table
                  columns={installedColumns}
                  rows={servers}
                  rowKey={(m) => m.id}
                  dense
                  empty="You have not installed any MCPs."
                />
              ) : (
                <Awaiting note={serversQuery.isError ? 'Could not load installed MCPs.' : 'Loading installed MCPs…'} />
              )) : (tentacles ? (
                <Table
                  columns={presetColumns}
                  rows={tentacles}
                  rowKey={(t) => t.id}
                  dense
                  empty="No configured tentacles supply an MCP."
                />
              ) : (
                <Awaiting note={tentaclesQuery.isError ? 'Could not load tentacles.' : 'Loading tentacles…'} />
              ))}
            </>
          )}

          {mgmtSec === 'profile' && <AccountPanel />}
          {mgmtSec === 'keys' && <ApiKeysPanel />}
          {mgmtSec === 'channels' && (profile ? (
            <Table
              className="trk-channel-table"
              columns={profileColumns}
              rows={profile.profiles}
              rowKey={(p) => p.id}
              dense
              empty="No channel identities are linked to your account."
            />
          ) : (
            <Awaiting note={profileQuery.isError ? 'Could not load channels.' : 'Loading channels…'} />
          ))}

          {mgmtSec === 'dash' && (
            <div className="trk-control-scroll">
              <div className="trk-dashboard-stats">
                {[
                  { label: 'Agents', value: agents?.length, detail: 'Registered tentacles', error: agentsQuery.isError },
                  { label: 'Model routes', value: routeCount, detail: 'Available to dispatch', error: agentsQuery.isError },
                  { label: 'Enabled MCPs', value: enabledCount, detail: 'Installed by you', error: serversQuery.isError },
                  { label: 'Channels', value: profile?.profiles.length, detail: 'Your channel identities', error: profileQuery.isError },
                ].map((stat) => (
                  <div key={stat.label}>
                    <span style={{ ...microSection, color: 'var(--fg-3)' }}>{stat.label}</span>
                    <div style={{ ...display(32), color: 'var(--fg-1)', margin: '8px 0' }}>{stat.value ?? '—'}</div>
                    <span style={{ ...mono(10), color: 'var(--fg-3)' }}>{stat.error ? 'Could not load' : stat.detail}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {mgmtSec === 'settings' && <SettingsPanel />}
        </div>

        <footer className="trk-control-footer">
          <span>{count === undefined ? page.title : `${count} records`}</span>
          <span>Trunkline</span>
        </footer>
      </div>
      {selectedProfile && (
        <ChannelProfilePanel
          profile={selectedProfile}
          onClose={() => {
            setSelectedProfileId(null)
            profileTrigger.current?.focus()
          }}
        />
      )}
    </div>
  )
}
