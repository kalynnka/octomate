import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { LedgerItem } from '../src/lib/api/types.ts'
import { chatRows, type LocalNotice } from '../src/features/chat/ledger.ts'

const earlier: LedgerItem = { kind: 'user', uid: 'earlier', who: 'operator', t: '', text: 'Earlier message' }
const reply: LedgerItem = { kind: 'stream', uid: 'reply', text: 'Reply', streaming: true }
const later: LedgerItem = { kind: 'user', uid: 'later', who: 'operator', t: '', text: 'Later message' }
const notice: LocalNotice = { kind: 'notice', uid: 'error', text: 'Answer did not go through', tone: 'error', position: 2 }

test('local notices stay between preceding and later messages', () => {
  assert.deepEqual(chatRows([earlier], [reply], [notice]), [earlier, reply, notice])
  assert.deepEqual(chatRows([earlier], [reply, later], [notice]), [earlier, reply, notice, later])
})

test('settling live rows into history keeps notices at the same position', () => {
  const saved = { ...reply, uid: 'saved-reply', streaming: false }
  assert.deepEqual(chatRows([earlier, saved, later], [], [notice]), [earlier, saved, notice, later])
})

test('notice positions exclude duplicate live rows and transient dots', () => {
  assert.equal(chatRows([earlier], [earlier, reply, { kind: 'dots', uid: 'dots', label: 'Working' }]).length, 2)
})

test('notices arriving at the same position retain their arrival order', () => {
  const info: LocalNotice = { ...notice, uid: 'info', text: 'Effort changed', tone: 'info' }
  assert.deepEqual(chatRows([earlier], [reply, later], [notice, info]), [earlier, reply, notice, info, later])
})

test('a notice in an empty thread remains before its first message', () => {
  const opening = { ...notice, position: 0 }
  assert.deepEqual(chatRows([], [later], [opening]), [opening, later])
})
