/**
 * The composer's slash commands: what a line that starts with `/` asks for.
 * The gateway's own ops come first, then the commands the thread's agent offers
 * in its runtime; any other text is still a directive.
 */
import type { ApiCommandDescriptor } from '@/lib/api/events'

export type GatewayOp = 'summon' | 'teleport' | 'effort' | 'new'

export interface CommandParameter {
  name: string
  required: boolean
  /** Named option whose value can appear alongside positional arguments. */
  flag?: string
  /** A dedicated value control replaces the argument's suggestion buttons. */
  control?: 'slider' | 'models'
  /** This parameter consumes the remaining text, rather than one quoted or unquoted word. */
  rest?: boolean
}

export type Command = {
  /** Explicit input structure, when known. A free-form runtime hint is not a schema. */
  parameters?: CommandParameter[]
  description: string
} & (
  | { name: GatewayOp; native?: undefined }
  /** the agent's own command, run in its runtime rather than by the gateway */
  | { name: string; native: ApiCommandDescriptor }
)

export const COMMANDS: Command[] = [
  { name: 'summon', parameters: [{ name: 'agent', required: true }, { name: 'model', required: false, flag: '--model', control: 'models' }, { name: 'effort', required: false, flag: '--effort', control: 'slider' }], description: 'Let another agent take over from a prepared brief. Opens the summon composer.' },
  { name: 'teleport', parameters: [{ name: 'destination', required: false }], description: 'Carry this chat to another destination with the same agent and history.' },
  { name: 'effort', parameters: [{ name: 'level', required: false, control: 'slider' }], description: 'Set the reasoning effort the next turns of this conversation run at. Omit the level to use the default.' },
  { name: 'new', parameters: [], description: 'Open a fresh thread on this surface.' },
]

/** An agent's own command, read the way the finder reads a gateway op. */
export function nativeCommand(descriptor: ApiCommandDescriptor): Command {
  return {
    name: COMMANDS.some((one) => one.name === descriptor.name) ? `native:${descriptor.name}` : descriptor.name,
    description: descriptor.description,
    native: descriptor,
  }
}

/** One thing a command's argument can be. */
export interface Argument {
  value: string
  about: string
}
export interface ArgumentMatch {
  argument: Argument
  hits: number[]
}

/** A line read as a command: still naming it, or on to its argument. */
export type CommandLine =
  | { phase: 'name'; typed: string; matches: { command: Command; hits: number[] }[] }
  | { phase: 'argument'; command: Command; typed: string }

/** Keep upstream hints verbatim; only explicit metadata defines requiredness. */
export function commandHint(command: Command): string {
  return command.parameters?.map((one) => `${one.required ? '<' : '['}${one.flag ? `${one.flag} <${one.name}>` : one.name}${one.rest ? '…' : ''}${one.required ? '>' : ']'}`).join(' ')
    ?? command.native?.argument_hint ?? ''
}

/** Where each letter of `typed` falls in `text`, in order; null when one does not. */
export function fuzzy(typed: string, text: string): number[] | null {
  const lower = text.toLowerCase()
  const hits: number[] = []
  for (const letter of typed.toLowerCase()) {
    const at = lower.indexOf(letter, (hits.at(-1) ?? -1) + 1)
    if (at < 0) return null
    hits.push(at)
  }
  return hits
}

/** `text` cut into stretches that were typed and stretches that were not. */
export function highlight(text: string, hits: number[]): { text: string; hit: boolean }[] {
  const stretches: { text: string; hit: boolean }[] = []
  for (const [at, letter] of [...text].entries()) {
    const hit = hits.includes(at)
    const last = stretches.at(-1)
    if (last?.hit === hit) last.text += letter
    else stretches.push({ text: letter, hit })
  }
  return stretches
}

/** The command a line spells out of `commands`, or null when the line is a
 *  directive. An agent's own command takes whatever follows it, as typed. */
export function readCommand(text: string, commands: Command[]): CommandLine | null {
  if (!text.startsWith('/')) return null
  const space = text.search(/\s/)
  if (space < 0) {
    const typed = text.slice(1)
    const matches = commands.flatMap((command) => {
      const hits = fuzzy(typed, command.name)
      return hits ? [{ command, hits }] : []
    })
    matches.sort((a, b) => Number(b.command.name.toLowerCase() === typed.toLowerCase()) - Number(a.command.name.toLowerCase() === typed.toLowerCase()))
    return matches.length ? { phase: 'name', typed, matches } : null
  }
  const name = text.slice(1, space)
  const command = commands.find((one) => one.name === name)
    ?? commands.find((one) => one.name.toLowerCase() === name.toLowerCase())
  if (!command || (!command.native && (text.includes('\n') || !command.parameters?.length))) return null
  return { phase: 'argument', command, typed: text.slice(space + 1) }
}

/** The arguments what was typed could still become. */
export function matching(typed: string, offered: Argument[]): ArgumentMatch[] {
  const needle = typed.trim().replace(/\s+/g, ' ').toLowerCase()
  return offered.flatMap((argument) => {
    const hits = fuzzy(needle, argument.value)
    return hits ? [{ argument, hits }] : []
  }).sort((a, b) => Number(b.argument.value.toLowerCase() === needle) - Number(a.argument.value.toLowerCase() === needle))
}

/** Resolve declared positional and named inputs; unknown native syntax remains opaque. */
export function commandArguments(
  line: Extract<CommandLine, { phase: 'argument' }>,
  choices: Record<string, Argument[]> = {},
  defaults: Record<string, string> = {},
) {
  const parameters = line.command.parameters ?? []
  const tokens = [...line.typed.matchAll(/(?:[^\s"'\\]|\\[\s\S]|"(?:[^"\\]|\\[\s\S])*"?|'[^']*'?)+/g)]
  const positional = parameters.flatMap((parameter, index) => parameter.flag ? [] : [index])
  const flags = parameters.flatMap((parameter) => parameter.flag ? [{ value: parameter.flag, about: parameter.name }] : [])
  const slots = new Map<number, number>()
  const values: Record<string, string> = {}
  const missing: string[] = []
  const invalid: string[] = []
  let position = 0
  let pending: number | undefined
  let lastAt: number | undefined
  let flagName = false
  for (const [index, token] of tokens.entries()) {
    flagName = flags.length > 0 && token[0].startsWith('--')
    if (flagName) {
      if (pending !== undefined) invalid.push(parameters[pending].name)
      const at = parameters.findIndex((parameter) => parameter.flag === token[0])
      pending = at < 0 ? undefined : at
      if (at < 0) invalid.push(token[0])
      flagName = at < 0
      continue
    }
    lastAt = pending ?? positional[position++]
    pending = undefined
    if (lastAt === undefined) {
      if (line.command.parameters) invalid.push('extra arguments')
      continue
    }
    slots.set(lastAt, index)
    if (parameters[lastAt].rest) break
  }
  if (pending !== undefined) invalid.push(parameters[pending].name)
  for (const [index, parameter] of parameters.entries()) {
    const slot = slots.get(index)
    const token = slot === undefined ? undefined : tokens[slot]
    const raw = parameter.rest ? line.typed.slice(token?.index ?? line.typed.length) : token?.[0] ?? ''
    const value = parameter.rest ? raw : raw.replace(/"((?:[^"\\]|\\[\s\S])*)"?|'([^']*)'?|\\([\s\S])/g, (_match, double: string | undefined, single: string | undefined, escaped: string | undefined) => double?.replace(/\\([\s\S])/g, '$1') ?? single ?? escaped ?? '')
    const options = choices[parameter.name]
    const resolved = value && options ? matching(value, options)[0]?.argument.value : value
    values[parameter.name] = resolved || defaults[parameter.name] || ''
    if (parameter.required && !values[parameter.name]) missing.push(parameter.name)
    if (value && options && !resolved) invalid.push(parameter.name)
  }
  const last = tokens.at(-1)
  const atEnd = last && last.index + last[0].length === line.typed.length
  const at = pending ?? (lastAt !== undefined && parameters[lastAt].rest ? lastAt : atEnd ? lastAt : positional[position])
  const active = flagName || at === undefined ? undefined : parameters[at]
  const slot = at === undefined ? undefined : slots.get(at)
  const start = flagName ? last?.index ?? line.typed.length : pending !== undefined || slot === undefined ? line.typed.length : tokens[slot].index
  const typed = line.typed.slice(start)
  const offered = flagName || !active ? flags.filter((option) => !tokens.some((token) => token[0] === option.value)) : choices[active.name] ?? []
  return { values, missing, invalid, active, at, typed, start, flagName, needsSpace: pending !== undefined && atEnd, offered, matches: matching(typed.replace(/^["']|["']$/g, ''), offered) }
}

/** Edit declared arguments without encoding command-specific positions in the UI. */
export function editArguments(line: Extract<CommandLine, { phase: 'argument' }>, update: Record<string, string>): string {
  const { values } = commandArguments(line)
  const argumentsText = line.command.parameters?.map((parameter) => {
    const value = update[parameter.name] ?? values[parameter.name]
    const quoted = value && /[\s"'\\]/.test(value) && !parameter.rest ? JSON.stringify(value) : value
    return parameter.flag && quoted ? `${parameter.flag} ${quoted}` : quoted
  }).filter(Boolean).join(' ') ?? line.typed
  return `/${line.command.name} ${argumentsText}`
}

/** Tab inserts real names or values, never the hint's placeholder text. */
export function completeCommand(line: CommandLine, matches: ArgumentMatch[], at: number): string {
  if (line.phase === 'name') {
    const command = line.matches[at].command
    return `/${command.name}${commandHint(command) || command.native ? ' ' : ''}`
  }
  const args = commandArguments(line)
  const value = matches[at]?.argument.value
  if (value && line.command.parameters?.some((parameter) => parameter.flag === value)) {
    return `/${line.command.name} ${line.typed.slice(0, args.start)}${value} `
  }
  if (!args.active || value === undefined) return `/${line.command.name} ${line.typed}`
  const more = line.command.parameters?.some((parameter, index) => parameter !== args.active && (parameter.flag ? !args.values[parameter.name] : index > (args.at ?? -1)))
  return editArguments(line, { [args.active.name]: value }) + (more ? ' ' : '')
}

/** What Tab would still add: the rest of the name or argument under the cursor. */
export function completion(line: CommandLine, matches: ArgumentMatch[], at: number): string {
  if (line.phase === 'name') {
    const { command } = line.matches[at]
    if (!command.name.toLowerCase().startsWith(line.typed.toLowerCase())) return ''
    const hint = commandHint(command)
    return command.name.slice(line.typed.length) + (hint && ` ${hint}`)
  }
  if (!line.command.parameters) return line.typed.trim() ? '' : commandHint(line.command)
  const args = commandArguments(line)
  const typed = args.typed
  const value = matches[at]?.argument.value
  if (value?.toLowerCase().startsWith(typed.toLowerCase())) {
    const hint = args.flagName || args.active?.flag ? '' : commandHint({ ...line.command, parameters: line.command.parameters.slice((args.at ?? line.command.parameters.length) + 1).filter((parameter) => !parameter.flag || !args.values[parameter.name]) })
    return (args.needsSpace ? ' ' : '') + value.slice(typed.length) + (hint ? ` ${hint}` : '')
  }
  return typed ? '' : commandHint({ ...line.command, parameters: line.command.parameters.slice(args.at ?? line.command.parameters.length) })
}
