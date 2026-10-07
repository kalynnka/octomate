import assert from 'node:assert/strict'
import { test } from 'node:test'
import { COMMANDS, commandArguments, commandHint, completeCommand, completion, editArguments, nativeCommand, readCommand, type Command, type CommandLine } from '../src/features/chat/commands.ts'

const native = (name: string, hint: string | null) => nativeCommand({
  id: `skill:${name}`, name, description: 'A runtime command', argument_hint: hint, requires_conversation: true, accepts_attachments: null,
})
const deploy: Command = {
  ...native('deploy', null),
  parameters: [{ name: 'environment', required: true }, { name: 'region', required: true }, { name: 'strategy', required: false }],
}
const choices = {
  environment: [{ value: 'staging', about: 'Preview' }, { value: 'production', about: 'Live' }],
  region: [{ value: 'east', about: '' }, { value: 'west', about: '' }],
  strategy: [{ value: 'rolling', about: '' }, { value: 'replace', about: '' }],
}
const read = (typed: string, command = deploy): Extract<CommandLine, { phase: 'argument' }> => ({ phase: 'argument', command, typed })

test('any declared command completes successive required and optional parameters', () => {
  assert.equal(commandHint(deploy), '<environment> <region> [strategy]')
  for (const [typed, hint, inserted] of [
    ['', 'staging <region> [strategy]', '/deploy staging '],
    ['stag', 'ing <region> [strategy]', '/deploy staging '],
    ['staging ', 'east [strategy]', '/deploy staging east '],
    ['staging ea', 'st [strategy]', '/deploy staging east '],
    ['staging east ', 'rolling', '/deploy staging east rolling'],
  ]) {
    const line = read(typed)
    const args = commandArguments(line, choices)
    assert.equal(completion(line, args.matches, 0), hint)
    assert.equal(completeCommand(line, args.matches, 0), inserted)
  }
})

test('requiredness, defaults and invalid values come from metadata rather than command names', () => {
  assert.deepEqual(commandArguments(read(''), choices).missing, ['environment', 'region'])
  assert.deepEqual(commandArguments(read('staging'), choices).missing, ['region'])
  const ready = commandArguments(read('staging east'), choices, { strategy: 'rolling' })
  assert.deepEqual(ready.values, { environment: 'staging', region: 'east', strategy: 'rolling' })
  assert.deepEqual(ready.missing, [])
  assert.deepEqual(ready.invalid, [])
  assert.deepEqual(commandArguments(read('staging east unknown'), choices).invalid, ['strategy'])
  assert.deepEqual(commandArguments(read('staging east rolling extra'), choices).invalid, ['extra arguments'])
})

test('editing one parameter preserves other values, quoted words and a free-text remainder', () => {
  const message: Command = { ...native('notify', null), parameters: [{ name: 'recipient', required: true }, { name: 'message', required: false, rest: true }] }
  const line = read('"Team Alpha" keep  "these quotes" and spaces', message)
  assert.deepEqual(commandArguments(line).values, { recipient: 'Team Alpha', message: 'keep  "these quotes" and spaces' })
  assert.equal(editArguments(line, { recipient: 'Team Beta' }), '/notify "Team Beta" keep  "these quotes" and spaces')
  assert.equal(editArguments(read('staging east rolling'), { strategy: 'replace' }), '/deploy staging east replace')
  assert.deepEqual(commandArguments(read('Team\\ Alpha hi', message)).values, { recipient: 'Team Alpha', message: 'hi' })
})

test('native hints are shown verbatim without inventing constraints, choices or arguments', () => {
  for (const hint of [null, '', 'target', '[issue description]', '[off|message]', '<optional custom summarization instructions>', '<file> [instructions]']) {
    const command = native('runtime-command', hint)
    const line = read('', command)
    assert.equal(commandHint(command), hint ?? '')
    assert.equal(completion(line, [], 0), hint ?? '')
    assert.deepEqual(commandArguments(line).missing, [])
    assert.deepEqual(commandArguments(line).matches, [])
    assert.equal(completeCommand(line, [], 0), '/runtime-command ')
    assert.equal(completeCommand(read('raw  "input"', command), [], 0), '/runtime-command raw  "input"')
  }
})

test('skills and namespaced native commands preserve raw multiline input', () => {
  const skill = native('plugin:review', null)
  const input = '  --flag "keep  spaces"\nnext line\n'
  const line = readCommand(`/plugin:review ${input}`, [skill])
  assert.equal(line?.phase === 'argument' && line.typed, input)
  const command = native('effort', '[off|message]')
  assert.equal(command.name, 'native:effort')
  const selected = readCommand('/native:effort off', [...COMMANDS, command])
  assert.equal(selected?.phase === 'argument' && selected.command.native?.id, 'skill:effort')
  assert.equal(readCommand('/summon\nclaude', COMMANDS), null)
})

test('exact names precede fuzzy matches and case-insensitive hints agree with Tab', () => {
  const line = readCommand('/review', [native('review-extra', null), native('review', null)])
  assert.equal(line?.phase === 'name' && line.matches[0].command.name, 'review')
  const upper = readCommand('/REV', [native('review', '[target]')])!
  assert.equal(completion(upper, [], 0), 'iew [target]')
  assert.equal(completeCommand(upper, [], 0), '/review ')
  const input = readCommand('/REVIEW keep This Case', [native('review', '[target]')])
  assert.equal(input?.phase === 'argument' && input.typed, 'keep This Case')
})

test('named options resolve in either order and preserve positional arguments', () => {
  const command: Command = { ...deploy, parameters: [
    { name: 'environment', required: true },
    { name: 'region', required: false, flag: '--region' },
    { name: 'strategy', required: false, flag: '--strategy' },
  ] }
  for (const typed of ['staging --region ea --strategy roll', '--strategy roll staging --region ea', 'staging --region "east" --strategy rolling']) {
    const line = read(typed, command)
    const args = commandArguments(line, choices)
    assert.deepEqual(args.values, { environment: 'staging', region: 'east', strategy: 'rolling' })
    assert.deepEqual(args.invalid, [])
    assert.deepEqual(args.missing, [])
    assert.equal(editArguments(line, { region: 'west', strategy: 'replace' }), '/deploy staging --region west --strategy replace')
  }
  assert.deepEqual(commandArguments(read('staging', command), choices, { strategy: 'rolling' }).values, { environment: 'staging', region: '', strategy: 'rolling' })
  for (const typed of ['staging --region', 'staging --region --strategy rolling', 'staging --region nowhere']) {
    assert.ok(commandArguments(read(typed, command), choices).invalid.includes('region'))
  }
  assert.deepEqual(commandArguments(read('staging east', command), choices).invalid, ['extra arguments'])
})

test('named options complete flags and fuzzy values without repeating supplied options', () => {
  const command = COMMANDS.find((one) => one.name === 'summon')!
  const offered = {
    agent: [{ value: 'claude', about: '' }],
    model: ['sonnet-plus', 'sonnet', 'haiku'].map((value) => ({ value, about: '' })),
    effort: ['low', 'high'].map((value) => ({ value, about: '' })),
  }
  for (const [typed, expected] of [
    ['claude --mo', '/summon claude --model '],
    ['claude --model', '/summon claude --model sonnet-plus '],
    ['claude --model snp', '/summon claude --model sonnet-plus '],
    ['claude --effort hi --model sonnet', '/summon claude --model sonnet --effort hi'],
    ['claude --model sonnet --effort h', '/summon claude --model sonnet --effort high'],
  ]) {
    const line = read(typed, command)
    const args = commandArguments(line, offered)
    assert.equal(completeCommand(line, args.matches, 0), expected)
  }
  const flag = read('claude --model', command)
  assert.equal(completion(flag, commandArguments(flag, offered).matches, 0), ' sonnet-plus')
  const options = commandArguments(read('claude --model sonnet ', command), offered)
  assert.deepEqual(options.matches.map((one) => one.argument.value), ['--effort'])
  const selected = commandArguments(read('claude --model sonnet --effort high', command), offered)
  assert.deepEqual(selected.values, { agent: 'claude', model: 'sonnet', effort: 'high' })
  assert.deepEqual(selected.invalid, [])
})
