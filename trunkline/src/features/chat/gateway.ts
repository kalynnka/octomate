/**
 * What the composer holds while it is expanded for a gateway op, and how that
 * becomes the request. Kept out of the components so it reads without them.
 */
import { useState, type KeyboardEvent } from 'react'
import type { ApiAgentRoute, ChannelAddress, GatewayRequest } from '@/lib/api/events'
import type { ChannelMeta } from '@/lib/api/types'
import { ApiError } from '@/lib/api/auth'
import { COMMANDS, readCommand } from './commands'

export type GatewayAction = GatewayRequest['action']

/** Move focus between menu choices without taking text editing or range keys. */
export function navigateGatewayMenu(event: KeyboardEvent<HTMLElement>) {
  if (event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return
  if (event.target instanceof HTMLInputElement && (event.target.type === 'range' || event.key === 'Home' || event.key === 'End')) return
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
  const choices = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[data-gateway-option]:not(:disabled)')]
  if (!choices.length) return
  const at = choices.findIndex((choice) => choice === event.target)
  const next = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1
    : event.key === 'ArrowDown' ? (at + 1) % choices.length
      : (at <= 0 ? choices.length : at) - 1
  event.preventDefault()
  event.stopPropagation()
  choices[next].focus()
}

/** A slash command still being completed must not become an operation's prompt. */
export function isGatewayCommand(text: string, action: GatewayAction): boolean {
  const line = readCommand(text, COMMANDS)
  return line?.phase === 'name'
    ? line.matches.some((one) => one.command.name === action)
    : line?.command.name === action
}

/** The same destination refusal or empty result in the finder and the menu. */
export function destinationReason(error: Error | null, noun: string): string {
  if (!error) return `No ${noun} to list here.`
  return error instanceof ApiError && error.status === 409
    ? error.message : `Couldn't load ${noun} — ${error.message}`
}

/** The effort scale as the slider draws it; `auto` leaves the agent its default. */
export type EffortLevel = string

/** A picked address and the labels walked to reach it. */
export interface Destination {
  address: ChannelAddress
  path: string[]
}

export interface GatewayForm {
  agent: string | null
  model: string | null
  effort: EffortLevel
  destination: Destination | null
  /** how far the destination browser has been opened */
  crumbs: Crumb[]
  menu: 'route' | 'destination' | null
}

// The line that opens the destination.
const HINTS: Record<GatewayAction, string> = {
  teleport: 'Continuing this conversation here.',
  summon: 'Continuing with another agent.',
}

const blank = (action: GatewayAction | null): GatewayForm => ({
  agent: null,
  model: null,
  effort: 'auto',
  destination: null,
  crumbs: [],
  // Teleport has nothing to send until it has somewhere to go.
  menu: action === 'teleport' ? 'destination' : null,
})

/** Both entry points share a draft, kept when closed until the thread or op changes. */
export function useGatewayForm(threadId: string, action: GatewayAction | null) {
  const [held, setHeld] = useState({ threadId, action, form: blank(action) })
  const changed = held.threadId !== threadId || (action !== null && held.action !== action)
  if (changed) setHeld({ threadId, action, form: blank(action) })
  const form = changed ? blank(action) : held.form
  const patch = (change: Partial<GatewayForm>) => setHeld((now) => ({ ...now, form: { ...now.form, ...change } }))
  // What a command opens an op with; set in the same turn the composer switches to it.
  const seed = (next: GatewayAction, change: Partial<GatewayForm>) =>
    setHeld((now) => ({
      threadId, action: next,
      form: { ...(now.threadId === threadId && now.action === next ? now.form : blank(next)), ...change },
    }))
  return [form, patch, seed] as const
}

/** The picked route, or the first one offered while nothing is picked. */
export function pickRoute(routes: ApiAgentRoute[], agent: string | null, model: string | null) {
  return routes.find((one) => one.agent_id === agent && one.model === model)
    ?? routes.find((one) => one.agent_id === agent)
    ?? routes[0]
}

/** One step into a channel: the channel itself, then each place opened in it. */
export interface Crumb {
  label: string
  channel: string
  inside?: string
}

export interface DestinationRow {
  key: string
  label: string
  sub: string
  glyph: string
  brand?: string
  here?: boolean
  /** set on a place to open */
  open?: Crumb
  /** set on an address to land in */
  address?: ChannelAddress
  /** why the address cannot be picked */
  barred?: string
}

// What each level of a channel holds, by channel id like `channelMeta`'s table;
// a channel absent here reads as plain destinations.
const LEVELS: Record<string, { many: string; one: string }[]> = {
  discord: [{ many: 'servers', one: 'server' }, { many: 'channels', one: 'text channel' }],
  slack: [{ many: 'channels', one: 'channel' }],
  lark: [{ many: 'groups', one: 'group' }],
}
export const level = (channel: string, depth: number) => LEVELS[channel]?.[depth] ?? { many: 'destinations', one: 'destination' }

/**
 * The browser's first level: each connected channel, to open or to land in.
 * A channel nothing can land in is barred with the relay's reason, and one the
 * op has no route on with `unrouted`.
 */
export function channelRows(
  channels: ChannelMeta[], suggestions: ChannelAddress[], sourceChannel: string,
  routes: Record<string, ApiAgentRoute[]>, unrouted: string, barred: Record<string, string>,
): DestinationRow[] {
  return channels.map((channel) => {
    // A channel that lands straight in a thread of its own has nothing to open.
    const direct = suggestions.find((one) => one.channel_tentacle_id === channel.id && one.chat_type === 'thread')
    const row = { key: channel.id, label: channel.label, glyph: channel.label[0], brand: channel.brand, here: channel.id === sourceChannel }
    const closed = barred[channel.id] ?? (routes[channel.id]?.length ? undefined : unrouted)
    if (closed) return { ...row, sub: closed, barred: closed }
    if (direct) return { ...row, sub: 'a new private thread', address: direct }
    return { ...row, sub: `${channel.sub} · ${level(channel.id, 0).many}`, open: { label: channel.label, channel: channel.id } }
  })
}

/** How a listed address reads in the browser, at the level `crumbs` has reached. */
export function addressRow(address: ChannelAddress, crumbs: Crumb[]): DestinationRow {
  const { channel } = crumbs[0]
  const { name, inside, server, barred } = address.metadata ?? {}
  const kind = level(channel, crumbs.length - 1)
  const key = `${address.chat_type}:${address.chat_id}:${inside ?? ''}`
  if (inside !== undefined) {
    const title = name ?? inside
    return {
      key, label: title, sub: `${kind.one} · open to list its ${level(channel, crumbs.length).many}`,
      glyph: title.split(/\s+/).map((word) => word[0]).join('').slice(0, 2).toUpperCase(),
      open: { label: title, channel, inside },
    }
  }
  if (address.chat_type === 'group') {
    const lands = address.shared ? 'a new thread everyone there can read' : 'a new private thread starts here'
    return { key, label: `#${name ?? address.chat_id}`, sub: barred ?? `${server ?? kind.one} · ${lands}`, glyph: '#', address, barred }
  }
  if (address.chat_type === 'dm') {
    return { key, label: 'Direct message', sub: barred ?? 'a new thread starts in your direct messages', glyph: '@', address, barred }
  }
  return { key, label: name ?? 'New thread', sub: barred ?? 'a new private thread', glyph: '+', address, barred }
}

/** What a route's effort scale offers and where it sits. A route that says what
 *  an unset effort runs at starts there with no Auto; the rest keep Auto. */
export function routeEffort(route: ApiAgentRoute | undefined, held: EffortLevel) {
  const claimed = route?.claim.efforts ?? []
  const preset = route?.claim.default_effort ?? null
  const efforts: EffortLevel[] = preset ? claimed : ['auto', ...claimed]
  return { efforts, effort: efforts.includes(held) ? held : preset ?? 'auto' }
}

export function sameAddress(a: ChannelAddress, b: ChannelAddress): boolean {
  return a.channel_tentacle_id === b.channel_tentacle_id
    && a.chat_type === b.chat_type
    && a.chat_id === b.chat_id
    && (a.channel_thread_id ?? null) === (b.channel_thread_id ?? null)
}

/**
 * The request the form stands for, or null while it is missing a part.
 * Teleport moves the conversation to the destination picked; Summon hands it to
 * another agent where it is.
 */
export function gatewayRequest(
  action: GatewayAction,
  form: { text: string; destination: Destination | null; route: ApiAgentRoute | undefined; effort: EffortLevel },
): GatewayRequest | null {
  if (isGatewayCommand(form.text, action)) return null
  const text = form.text.trim()
  if (action === 'teleport') {
    if (!form.destination) return null
    return { action, body: { destination: form.destination.address, hint: HINTS.teleport, ...(text ? { prompt: text } : {}) } }
  }
  if (!form.route || !text) return null
  return {
    action,
    body: {
      agent_id: form.route.agent_id,
      model: form.route.model,
      brief: text,
      hint: HINTS.summon,
      ...(form.effort === 'auto' ? {} : { effort: form.effort }),
    },
  }
}
