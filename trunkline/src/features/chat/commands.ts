/**
 * The composer's slash commands: what a line that starts with `/` asks for.
 * Only the gateway ops are commands; any other text is still a directive.
 */

export interface Command {
  name: 'summon' | 'teleport'
  /** how its argument reads: `<needed>`, `[optional]`, or empty for none */
  takes: string
  /** what its argument is picked from, in a word */
  offers: string
  description: string
}

export const COMMANDS: Command[] = [
  { name: 'summon', takes: '<agent>', offers: 'agent', description: 'Let another agent take over from a prepared brief. Opens the summon composer.' },
  { name: 'teleport', takes: '[destination]', offers: 'surface', description: 'Carry this chat to another destination with the same agent and history.' },
]

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

/** The command a line spells, or null when the line is a directive. */
export function readCommand(text: string): CommandLine | null {
  if (!text.startsWith('/') || text.includes('\n')) return null
  const space = text.indexOf(' ')
  if (space < 0) {
    const typed = text.slice(1)
    const matches = COMMANDS.flatMap((command) => {
      const hits = fuzzy(typed, command.name)
      return hits ? [{ command, hits }] : []
    })
    return matches.length ? { phase: 'name', typed, matches } : null
  }
  const command = COMMANDS.find((one) => one.name === text.slice(1, space))
  return command?.takes ? { phase: 'argument', command, typed: text.slice(space + 1) } : null
}

/** The arguments what was typed could still become. */
export function matching(typed: string, offered: Argument[]): ArgumentMatch[] {
  return offered.flatMap((argument) => {
    const hits = fuzzy(typed.trim(), argument.value)
    return hits ? [{ argument, hits }] : []
  })
}

/** What Tab would still add: the rest of the name or argument under the cursor. */
export function completion(line: CommandLine, matches: ArgumentMatch[], at: number): string {
  if (line.phase === 'name') {
    const { command } = line.matches[at]
    if (!command.name.startsWith(line.typed)) return ''
    return command.name.slice(line.typed.length) + (command.takes && ` ${command.takes}`)
  }
  const typed = line.typed.trim()
  const value = matches[at]?.argument.value
  if (value?.toLowerCase().startsWith(typed.toLowerCase())) return value.slice(typed.length)
  return typed ? '' : line.command.takes
}
