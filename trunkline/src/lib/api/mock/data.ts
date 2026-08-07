/**
 * Mock dataset ported from the Trunkline Console design comp. Every record
 * mirrors a backend concept (threads, handoffs, sessions, deferred actions,
 * projects, users) so swapping in real endpoints is a data-source change only.
 */
import type {
  AgentRouteDef,
  ChannelMeta,
  ControlData,
  DocLine,
  DocLineKind,
  LedgerItem,
  ProjectInfo,
  ProjectRef,
  ReviewComment,
  ReviewDoc,
  StatusData,
  SurfaceInfo,
  ThreadDetail,
  ThreadSummary,
} from '../types'

export const channels: ChannelMeta[] = [
  { id: 'trunkline', label: 'Trunkline', sub: 'mention-free', brand: 'var(--color-accent)' },
  { id: 'slack', label: 'Slack', sub: 'socket', brand: '#746576' },
  { id: 'lark', label: 'Lark', sub: 'webhook', brand: '#666D82' },
  { id: 'napcat', label: 'Napcat', sub: 'ws', brand: '#6A828B' },
  { id: 'claude', label: 'Claude', sub: 'hook · tail', native: true, brand: '#98796A' },
  { id: 'codex', label: 'Codex', sub: 'hook · tail', native: true, brand: '#677D73' },
]

export const threads: Record<string, ThreadSummary[]> = {
  trunkline: [
    {
      id: 'THR-0198',
      channelId: 'trunkline',
      title: 'CI triage — failing staging checks',
      tone: 'active',
      agentLabel: 'inkling → claude',
    },
    {
      id: 'THR-0195',
      channelId: 'trunkline',
      title: 'migration plan review — registry',
      tone: 'idle',
      agentLabel: 'claude · opus[1m]',
    },
  ],
  slack: [
    {
      id: 'THR-0142',
      channelId: 'slack',
      title: 'staging deploy failing since deploy/482',
      tone: 'active',
      agentLabel: 'inkling → claude',
    },
    {
      id: 'THR-0139',
      channelId: 'slack',
      title: 'flaky e2e: cart smoke times out',
      tone: 'idle',
      agentLabel: 'codex · gpt-5.3',
    },
    {
      id: 'THR-0136',
      channelId: 'slack',
      title: 'copy review — onboarding cards',
      tone: 'active',
      agentLabel: 'inkling · v4-flash',
    },
  ],
  lark: [
    {
      id: 'THR-0173',
      channelId: 'lark',
      title: 'weekly digest — relay roundup',
      tone: 'active',
      agentLabel: 'inkling · v4-flash',
    },
    {
      id: 'THR-0161',
      channelId: 'lark',
      title: 'lark card render bug on feelers',
      tone: 'active',
      agentLabel: 'codex · gpt-5.3',
    },
  ],
  napcat: [
    {
      id: 'THR-0184',
      channelId: 'napcat',
      title: 'weather brief for the ride in',
      tone: 'active',
      agentLabel: 'inkling · v4-flash',
    },
    {
      id: 'THR-0187',
      channelId: 'napcat',
      title: 'morning tarot pull → digest',
      tone: 'idle',
      agentLabel: 'inkling · v4-flash',
    },
  ],
  claude: [
    {
      id: 'THR-0201',
      channelId: 'claude',
      title: 'staging-fix — base-image bump PR',
      tone: 'native',
      agentLabel: 'claude code · atlas',
    },
  ],
  codex: [
    {
      id: 'THR-0203',
      channelId: 'codex',
      title: 'registry migration tidy-up',
      tone: 'native',
      agentLabel: 'codex rollout · atlas',
    },
  ],
}

export const surfaces: SurfaceInfo[] = [
  { id: 'trunkline', label: 'Trunkline', sub: 'direct · console', brand: 'var(--color-accent)' },
  { id: 'slack', label: 'Slack', sub: '#eng-platform · thread', brand: '#746576' },
  { id: 'lark', label: 'Lark', sub: 'ops group · oc_88f2', brand: '#666D82' },
  { id: 'napcat', label: 'Napcat', sub: 'qq group 594177…', brand: '#6A828B' },
]

export const projectRefs: Record<string, ProjectRef> = {
  'THR-0198': {
    name: 'maple-staging',
    path: '~/work/maple-staging',
    git: true,
    branch: 'deploy/482',
    stat: '↑2 · +4 −1',
    dirty: true,
    also: 'also filed here: THR-0142 · slack · THR-0201 · claude',
  },
  'THR-0142': {
    name: 'maple-staging',
    path: '~/work/maple-staging',
    git: true,
    branch: 'deploy/482',
    stat: '↑2 · +4 −1',
    dirty: true,
    also: 'also filed here: THR-0198 · web · THR-0201 · claude',
  },
  'THR-0201': {
    name: 'maple-staging',
    path: '~/work/maple-staging',
    git: true,
    branch: 'fix/base-image-bump',
    stat: '↑1 · clean',
    dirty: false,
    also: 'also filed here: THR-0198 · web · THR-0142 · slack',
  },
  'THR-0195': {
    name: 'registry-cutover',
    path: '~/work/registry-cutover',
    git: true,
    branch: 'main',
    stat: 'clean',
    dirty: false,
  },
  'THR-0203': {
    name: 'registry-tidy',
    path: '~/work/registry-tidy',
    git: true,
    branch: 'main',
    stat: '+2 −0',
    dirty: true,
  },
  'THR-0136': { name: 'onboarding-copy', path: '~/notes/onboarding-copy', git: false },
}

export const projectList: ProjectInfo[] = [
  {
    name: 'maple-staging',
    path: '~/work/maple-staging',
    git: true,
    sub: 'THR-0198 · THR-0142 · THR-0201 filed here',
    note: 'shared with THR-0198 · web · THR-0142 · slack · THR-0201 · claude',
    branches: [
      { n: 'deploy/482', stat: '↑2 · +4 −1', dirty: true },
      { n: 'fix/base-image-bump', stat: '↑1 · clean', dirty: false },
      { n: 'main', stat: 'clean', dirty: false },
    ],
  },
  {
    name: 'registry-cutover',
    path: '~/work/registry-cutover',
    git: true,
    sub: 'THR-0195 filed here',
    note: 'shared with THR-0195 · web',
    branches: [{ n: 'main', stat: 'clean', dirty: false }],
  },
  {
    name: 'registry-tidy',
    path: '~/work/registry-tidy',
    git: true,
    sub: 'THR-0203 filed here',
    note: 'shared with THR-0203 · lark',
    branches: [{ n: 'main', stat: '+2 −0', dirty: true }],
  },
  {
    name: 'onboarding-copy',
    path: '~/notes/onboarding-copy',
    git: false,
    sub: 'THR-0136 filed here — plain directory',
    note: 'shared with THR-0136 · slack',
    branches: [],
  },
]

export const agentRoutes: AgentRouteDef[] = [
  {
    name: 'inkling',
    code: 'IK',
    models: ['v4-flash', 'v4-max'],
    desc: 'light triage — routes, summarizes, answers fast; hands anything heavy to claude',
  },
  {
    name: 'claude',
    code: 'CL',
    models: ['opus-4.1', 'sonnet-4.6'],
    desc: 'deep reasoning — plans, reviews and long-running edits with tool use',
  },
  {
    name: 'codex',
    code: 'CX',
    models: ['gpt-5.3', 'gpt-5.3-mini'],
    desc: 'code specialist — mechanical refactors, test scaffolds, batch edits',
  },
]

/* ---------------------------------------------------------------------------
 * Review dossiers (THR-0195)
 * ------------------------------------------------------------------------- */

const L = (id: string, k: DocLineKind, t = '', mark: DocLine['mark'] = ''): DocLine => ({
  id,
  k,
  t,
  mark,
})

const planLines: DocLine[] = [
  L('a1', 'meta', 'rev: REV_02 · owner: claude · opus[1m] · thread THR-0195'),
  L('a2', 'meta', 'scope: user-registry cutover · staging → prod · pager: kalynnka'),
  L('a3', 'blank'),
  L('a4', 'h1', 'Registry cutover plan'),
  L(
    'a5',
    'p',
    'Move the user registry off the legacy pair without a write freeze — relay keeps both sides mirrored until parity holds for a full cycle.',
  ),
  L('a6', 'blank'),
  L('a7', 'h2', '01 · Preconditions'),
  L('a8', 'li', 'identity tables migrate ahead of oauth connections'),
  L('a9', 'li', 'dual-write shim deployed behind registry_mirror'),
  L('a10', 'li', 'parity probe green for 24h on staging'),
  L('a11', 'blank'),
  L('a12', 'h2', '02 · Mirror phase'),
  L(
    'a13',
    'p',
    'Writes land on legacy first; relay replays onto the new pair inside the same transaction envelope. Reads stay pinned to legacy.',
  ),
  L('a14', 'li', 'retry budget: 3 attempts, then drop the replay', 'del'),
  L('a15', 'li', 'retry budget: 5 attempts · exponential backoff, 250ms base', 'add'),
  L('a16', 'li', 'replays past 10k rows spill to spill_01J9 · kept 6h', 'add'),
  L('a17', 'li', 'parity sweep every 15m while the mirror holds'),
  L('a18', 'blank'),
  L('a19', 'h2', '03 · Cutover window'),
  L('a20', 'p', 'Window opens 02:00Z Saturday; pager holds the relay console open through the flip.'),
  L('a21', 'li', 'freeze schema changes 24h ahead of the window'),
  L('a22', 'li', 'flip reads: registry_read_target → new_pair'),
  L('a23', 'li', 'hold writes dual for one more cycle, then retire the shim'),
  L('a24', 'quote', 'rollback is a read-flip back to legacy — writes never left it.'),
  L('a25', 'blank'),
  L('a26', 'h2', '04 · Verification'),
  L('a27', 'num', '1 · row-count parity + sampled hash on 1% of rows'),
  L('a28', 'num', '2 · oauth token-exchange smoke across 3 providers'),
  L('a29', 'num', '3 · p95 read latency within 8% of the legacy baseline'),
  Object.assign(L('a30', 'code', 'SELECT count(*) FROM users_v2 EXCEPT SELECT count(*) FROM users;'), {
    lang: 'sql',
  }),
  L('a31', 'blank'),
  L('a32', 'h2', '05 · Fallout drills'),
  L(
    'a33',
    'p',
    'Two drills before the window: a replay-lag spike and a mid-flip pager handoff. Both end on the rehearsed read-flip rollback.',
  ),
  L('a34', 'hr'),
  L('a35', 'meta', '// end of plan · lines renumber on every rev · comments anchor to their lines'),
]

const runbookLines: DocLine[] = [
  L('r1', 'meta', 'rev: REV_01 · pager runbook · companion to REGISTRY_CUTOVER.md'),
  L('r2', 'blank'),
  L('r3', 'h1', 'Cutover runbook'),
  L(
    'r4',
    'p',
    'What the pager actually does while the plan executes. Keep the relay console open; every step lands in this thread.',
  ),
  L('r5', 'blank'),
  L('r6', 'h2', '01 · Before the window'),
  L('r7', 'li', 'silence non-cutover alerts for the window ± 1h'),
  L('r8', 'li', 'verify spill retention ≥ 6h on spill_01J9'),
  L('r9', 'li', 'dry-run the read-flip on staging, both directions'),
  L('r10', 'blank'),
  L('r11', 'h2', '02 · During the flip'),
  L('r12', 'li', 'watch the parity sweep — any red row halts the flip'),
  L('r13', 'li', 'latency probe posts to this thread every 5m'),
  L('r14', 'li', 'approvals ride the ledger — no side-channel yes'),
  L('r15', 'quote', 'if paged: read the last three ledger turns before touching anything.'),
  L('r16', 'hr'),
  L('r17', 'meta', '// end of runbook'),
]

export const reviewDocs: Record<string, ReviewDoc> = {
  'REGISTRY_CUTOVER.md': { rev: 'REV_02', lines: planLines },
  'cutover_runbook.md': { rev: 'REV_01', lines: runbookLines },
}

export const reviewComments: ReviewComment[] = [
  {
    id: 'CMT_01',
    file: 'REGISTRY_CUTOVER.md',
    anchor: 'a16',
    range: 'L14',
    who: 'kalynnka',
    t: '13:41',
    status: 'outdated',
    appliedIn: 'REV_02',
    text: 'retry budget too small — bump to 5 with backoff, and big replays need a spill path.',
  },
]

/* ---------------------------------------------------------------------------
 * Thread ledgers
 * ------------------------------------------------------------------------- */

const ciLedger: LedgerItem[] = [
  {
    kind: 'user',
    uid: 'ciu1',
    t: '13:58:02',
    who: 'kalynnka',
    text: "Pull the failing staging checks, figure out the root cause, and file an issue if it's ours.",
  },
  {
    kind: 'session-open',
    uid: 'ses-RUN_0521',
    sessionId: 'RUN_0521',
    text: 'RUN_0521 · inkling · v4-pro · session opened · 13:58:04',
    tone: 'plain',
  },
  {
    kind: 'think',
    uid: 'cithink',
    dur: '2.8s',
    text: 'Directive parsed. CI first — read the latest failing run, decide whether the break is ours or upstream. Deep log digging and any repo write is claude territory — likely a summon after the first look.',
  },
  {
    kind: 'plan',
    uid: 'ciplan',
    steps: [
      { n: '01', text: 'Fetch failing checks for deploy/482', status: 'done' },
      { n: '02', text: 'Diagnose root cause from CI logs', status: 'active' },
      { n: '03', text: 'File issue with findings, if ours', status: 'idle' },
    ],
  },
  {
    kind: 'tool',
    uid: 'cit1',
    name: 'gh.list_check_runs',
    icon: 'search',
    dur: '0.8s',
    status: 'done',
    detail: {
      type: 'checks',
      rows: [
        { n: '01', name: 'build-and-push', verdict: 'FAIL', dur: '3m41s' },
        { n: '02', name: 'e2e-smoke', note: '· skipped: dep', verdict: 'FAIL', dur: '0m12s' },
        { n: '03', name: 'lint-and-typecheck', verdict: 'PASS', dur: '1m03s' },
      ],
      summary: '3 checks · 0.8s',
    },
  },
  {
    kind: 'tool',
    uid: 'cit3',
    name: 'relay.summon',
    icon: 'summon',
    status: 'done',
    badge: { label: 'handover', tone: 'terra' },
    defaultOpen: true,
    detail: {
      type: 'summon',
      rows: [
        ['agent', 'claude · opus[1m] · effort high'],
        ['reason', 'log forensics + likely repo write — needs full CI context'],
        ['brief', 'findings so far + plan · thread ledger shared'],
      ],
    },
  },
  {
    kind: 'agent',
    uid: 'cimsg0',
    label: 'inkling · RUN_0521 · 13:59:14',
    blocks: [
      {
        type: 'p',
        text: 'Handed off. The brief travels with findings and the active plan, ledger stays shared — `SES-0522` owns the run from here.',
      },
    ],
  },
  {
    kind: 'session-open',
    uid: 'ses-SES-0522',
    sessionId: 'SES-0522',
    text: '⇄ summon · inkling → claude · opus[1m] · SES-0522 opened · 13:59:18',
    tone: 'summon',
  },
  {
    kind: 'tool',
    uid: 'cit2',
    name: 'ci.read_log',
    icon: 'file-diff',
    status: 'done',
    badge: { label: 'Spilled · 48.2k', tone: 'gold' },
    defaultOpen: true,
    detail: {
      type: 'log',
      heading: 'Result · reduced',
      note: 'preview 1,000 ch',
      pre: [
        { text: '#18 [build 12/14] RUN uv sync --frozen' },
        {
          text: "#18 ERROR: distribution `maple-core==2.4.1` can't be installed\nbecause the wheel requires python >=3.14, environment is 3.13",
          tone: 'error',
        },
        { text: '------\nDockerfile:31 — `FROM python:3.13-slim` pinned at base\n…' },
      ],
      spill: {
        label: 'Spill',
        text: '48,213 chars stored whole · handle spill_01J9FQ · readable 6h → read_tool_result(tail 4,000)',
      },
    },
  },
  {
    kind: 'approval',
    uid: 'ciappr',
    title: 'Create issue — maple/staging',
    desc: 'staging deploy broken — maple-core 2.4.1 wheel requires py3.14, base image pins 3.13.',
    meta: 'gh.create_issue · scope repo · approval resumes RUN_0521',
    state: 'waiting',
  },
  {
    kind: 'agent',
    uid: 'cimsg1',
    label: 'claude · SES-0522 · 14:00:41',
    blocks: [
      {
        type: 'p',
        text: "It's ours. The staging image pins `python:3.13-slim` while yesterday's `maple-core 2.4.1` release ships py3.14-only wheels — the build fails at dependency sync, and e2e is skipped downstream.",
      },
      {
        type: 'ul',
        items: [
          'Root cause: base-image / wheel version skew, introduced upstream at 09:12.',
          'Short fix: pin `maple-core==2.3.9` until the image bumps.',
          'Filed maple/staging#517 with the log excerpt and both options.',
        ],
      },
      {
        type: 'diff',
        diff: {
          file: 'Dockerfile',
          adds: 1,
          dels: 1,
          lines: [
            { tone: 'ctx', text: '# Dockerfile:31' },
            { tone: 'del', text: '- FROM python:3.13-slim' },
            { tone: 'add', text: '+ FROM python:3.14-slim' },
          ],
        },
      },
      {
        type: 'p',
        text: 'Want me to raise the base-image bump as a PR? That one needs another approval.',
      },
    ],
  },
  {
    kind: 'ask',
    uid: 'ciask',
    title: 'Base-image bump — raise as PR?',
    body: '#517 tracks the skew; the proper fix is FROM python:3.14-slim. The bump touches three images — staging, prod, and the nightly wheel-builder — and two transitive pins publish 3.14 wheels for linux/amd64 only: arm64 runners would fall back to source builds, adding ~11 min to CI (native-ext has failed that build twice). Prod rides Thursday’s release cut; staging can move any time.',
    options: [
      {
        label: 'Raise the PR',
        sum: 'FROM python:3.14-slim · needs one more approval',
        desc: 'Open the PR against maple/staging now. arm64 runners keep the 2.3.9 pin one more week so CI time stays flat; makes Thursday’s cut, but a canary trip rolls the bump back everywhere at once.',
      },
      {
        label: 'Pin maple-core==2.3.9',
        sum: 'unblocks staging in minutes · defers the real fix',
        desc: 'Land the pin directly on staging — no extra approval, deploy unblocks in minutes. The image bump then waits for next week’s cut and #517 stays open as the tracker.',
      },
    ],
    meta: 'relay.ask · scope thread · answer resumes RUN_0521',
    state: 'waiting',
  },
  {
    kind: 'end',
    uid: 'ciend',
    label:
      'End of turn · finished in 34s · 12,480 in / 1,904 out · 3 tools · 1 approval · 1 question · 1 spill',
  },
]

const ciHistory: LedgerItem[][] = [
  [
    { kind: 'divider', uid: 'h1d', label: 'Aug 1 · earlier in this thread' },
    {
      kind: 'user',
      uid: 'h1u1',
      t: '17:22',
      who: 'kalynnka',
      text: 'what changed in deploy/481? staging looked fine there',
    },
    {
      kind: 'agent',
      uid: 'h1a1',
      label: 'inkling · 17:23',
      blocks: [
        {
          type: 'p',
          text: '481 only bumped assets — the py-version skew arrives with 482 (maple-core 2.4.1). Diff summary posted.',
        },
      ],
    },
    { kind: 'user', uid: 'h1u2', t: '17:31', who: 'kalynnka', text: 'ok. keep an eye on the next deploy' },
  ],
  [
    { kind: 'divider', uid: 'h2d', label: 'Jul 30' },
    {
      kind: 'user',
      uid: 'h2u1',
      t: '10:04',
      who: 'kalynnka',
      text: 'sanity-check the user-registry migration order for me?',
    },
    {
      kind: 'agent',
      uid: 'h2a1',
      label: 'inkling · 10:05',
      blocks: [
        {
          type: 'p',
          text: 'checked — identity lands before oauth connections; safe. one index rename flagged to claude:',
        },
      ],
    },
    {
      kind: 'code',
      uid: 'h2c1',
      lang: 'sql',
      lines: [
        'CREATE INDEX idx_user_identity ON users (identity_key);',
        '-- rename: idx_uid → idx_user_identity (flagged)',
      ],
    },
  ],
  [
    { kind: 'divider', uid: 'h3d', label: 'Jul 28 · thread opened' },
    {
      kind: 'system',
      uid: 'h3s1',
      text: 'console thread THR-0198 opened · entry route inkling · v4-pro',
    },
    { kind: 'user', uid: 'h3u1', t: '09:14', who: 'kalynnka', text: 'so this console is the new relay surface?' },
    {
      kind: 'agent',
      uid: 'h3a1',
      label: 'inkling · 09:15',
      blocks: [
        {
          type: 'p',
          text: 'yes — mention-free, instant stream; threads from every channel land here.',
        },
      ],
    },
  ],
]

const planLedger: LedgerItem[] = [
  { kind: 'divider', uid: 'p0', label: 'Aug 4 · plan review' },
  { kind: 'system', uid: 'p1', text: 'thread THR-0195 · entry inkling → summon claude · opus[1m]' },
  {
    kind: 'user',
    uid: 'p2',
    t: '13:22',
    who: 'kalynnka',
    text: 'Draft the registry cutover plan as a file I can mark up — I want line comments, not a wall of chat.',
  },
  {
    kind: 'agent',
    uid: 'p2a',
    label: 'inkling · 13:22',
    blocks: [
      {
        type: 'p',
        text: 'Doc-shaped ask — summoning claude · opus[1m] to draft it as a reviewable dossier. Ledger shared; SES-0530 takes the run.',
      },
    ],
  },
  {
    kind: 'agent',
    uid: 'p3',
    label: 'claude · SES-0530 · 13:24',
    blocks: [
      {
        type: 'p',
        text: 'Plan drafted and pinned to this thread as a dossier. Open it, sweep across lines to quote or comment — every note folds into the next rev and the diff stays pinned to the last turn.',
      },
    ],
  },
  { kind: 'sub', uid: 'p3a', id: 'SES-0531', route: 'style-sweep · haiku', note: 'dossier voice pass · sealed' },
  {
    kind: 'sub',
    uid: 'p3b',
    id: 'SES-0532',
    route: 'fact-check · sonnet',
    note: 'migration order verified · sealed',
  },
  {
    kind: 'file',
    uid: 'p4',
    name: 'REGISTRY_CUTOVER.md',
    rev: 'REV_01',
    note: 'draft · 34 ln · click to open in the review panel',
  },
  { kind: 'file', uid: 'p5', name: 'cutover_runbook.md', rev: 'REV_01', note: 'pager runbook · 17 ln' },
  {
    kind: 'user',
    uid: 'p6',
    t: '13:41',
    who: 'kalynnka',
    text: 'Notes inline — fold them in.',
    chips: [
      {
        qid: 'CMT_01',
        kind: 'cmt',
        ref: 'CMT_01',
        label: 'CMT_01 · L14',
        body: 'retry budget too small — bump to 5 with backoff, and big replays need a spill path.',
      },
    ],
  },
  {
    kind: 'tool',
    uid: 'p7',
    name: 'plan.apply_edits',
    icon: 'send',
    status: 'done',
    detail: {
      type: 'plain',
      args: 'file: "REGISTRY_CUTOVER.md"\nnotes: CMT_01 · keep anchors',
      res: 'REV_02 applied · +2 −1\nCMT_01 → outdated · folded at anchor',
    },
  },
  {
    kind: 'sub',
    uid: 'p7a',
    id: 'SES-0533',
    route: 'plan-lint · haiku',
    note: '2 nits folded into REV_02 · sealed',
  },
  {
    kind: 'file',
    uid: 'p8',
    name: 'REGISTRY_CUTOVER.md',
    rev: 'REV_02',
    note: '+2 −1 vs REV_01 · CMT_01 folded',
  },
  {
    kind: 'agent',
    uid: 'p9',
    label: 'claude · SES-0530 · 13:43',
    blocks: [
      {
        type: 'p',
        text: 'REV_02 applied. The old retry line stays struck so you can see what moved; CMT_01 is stamped outdated and folded at its anchor. Flip Diff → Clean to read the plan straight — and the cutover window still reads loose to me if you want a pass on §03.',
      },
    ],
  },
  { kind: 'end', uid: 'p10', label: 'End of turn · REV_02 · +2 −1 · 1 note folded' },
]

export const threadDetails: Record<string, ThreadDetail> = {
  'THR-0198': {
    key: 'web / direct / console / —',
    msgCount: 14,
    sessions: [
      {
        n: '01',
        id: 'RUN_0521',
        route: 'inkling · v4-pro',
        kind: 'entry',
        t: '13:58',
        reason: 'entry route — console default, effort high',
        status: 'handed',
        tone: 'gold',
      },
      {
        n: '02',
        id: 'SES-0522',
        route: 'claude · opus[1m]',
        kind: 'summon',
        t: '13:59',
        reason: 'log forensics + likely repo write — brief packaged, ledger shared',
        status: 'active',
        tone: 'accent',
        anchor: 'ses-SES-0522',
      },
    ],
    ledger: ciLedger,
    history: ciHistory,
    project: projectRefs['THR-0198'],
    ctxK: 82,
    usage: { chip: 'Σ in 78.2k · cache 64.1k (82%) · out 18.2k', tok: '12.5k↑ 1.9k↓' },
  },
  'THR-0195': {
    key: 'web / direct / console / —',
    msgCount: 18,
    sessions: [
      {
        n: '01',
        id: 'RUN_0529',
        route: 'inkling · v4-pro',
        kind: 'entry',
        t: '13:22',
        reason: 'entry route — plan authorship needs claude',
        status: 'handed',
        tone: 'gold',
        anchor: 'p2',
      },
      {
        n: '02',
        id: 'SES-0530',
        route: 'claude · opus[1m]',
        kind: 'summon',
        t: '13:24',
        reason: 'plan authorship + review loop — file pinned, ledger shared',
        status: 'active',
        tone: 'accent',
        anchor: 'p3',
      },
    ],
    ledger: planLedger,
    history: [],
    project: projectRefs['THR-0195'],
    artifacts: [
      { name: 'REGISTRY_CUTOVER.md', ses: 'SES-0530', note: 'plan · review loop' },
      { name: 'cutover_runbook.md', ses: 'SES-0530', note: 'pager runbook' },
    ],
    docs: reviewDocs,
    comments: reviewComments,
    ctxK: 118,
    usage: { chip: 'Σ in 78.2k · cache 64.1k (82%) · out 18.2k', tok: '12.5k↑ 1.9k↓' },
  },
  'THR-0142': {
    key: 'slack / group / C04HQ2K / 1754120042.114',
    msgCount: 23,
    sessions: [
      {
        n: '01',
        id: 'SES-0516',
        route: 'inkling · v4-flash',
        kind: 'entry',
        t: '08:41',
        reason: 'entry route — channel default',
        status: 'sealed',
        tone: 'sage',
      },
      {
        n: '02',
        id: 'SES-0517',
        route: 'claude · opus[1m]',
        kind: 'summon',
        t: '08:43',
        reason: 'multi-file CI fix — brief packaged',
        status: 'sealed',
        tone: 'sage',
        hop: '⇄ teleport 09:12 · presentation → web · session preserved · pointer card left',
      },
    ],
    ledger: [],
    history: [],
    project: projectRefs['THR-0142'],
    ctxK: 82,
    usage: { chip: 'Σ in 24.1k · cache 18.0k (75%) · out 5.2k', tok: '3.1k↑ 0.8k↓' },
  },
  'THR-0201': {
    key: 'claude / atlas / ~/.claude/projects/staging-fix',
    msgCount: 9,
    sessions: [
      {
        n: '01',
        id: 'SES-0518',
        route: 'claude code · atlas',
        kind: 'ingest',
        t: '12:20',
        reason: 'hook-tailed native session — turns sealed into the ledger as they land',
        status: 'ingested',
        tone: 'teal',
      },
    ],
    ledger: [],
    history: [],
    project: projectRefs['THR-0201'],
    ctxK: 82,
    usage: { chip: 'Σ in 41.7k · cache 30.2k (72%) · out 8.8k', tok: '6.4k↑ 1.2k↓' },
  },
  'THR-0203': {
    key: 'codex / atlas / ~/.codex/sessions/…/rollout-88',
    msgCount: 6,
    sessions: [
      {
        n: '01',
        id: 'SES-0488',
        route: 'codex rollout · atlas',
        kind: 'ingest',
        t: '11:02',
        reason: 'env secret ✓ · offsets 0–12,884 checkpointed',
        status: 'ingested',
        tone: 'teal',
      },
    ],
    ledger: [],
    history: [],
    project: projectRefs['THR-0203'],
    ctxK: 82,
    usage: { chip: 'Σ in 12.9k · cache 8.4k (65%) · out 2.4k', tok: '1.9k↑ 0.4k↓' },
  },
}

export const defaultThreadDetail: ThreadDetail = {
  key: '— / — / — / —',
  msgCount: 4,
  sessions: [
    {
      n: '01',
      id: 'RUN_0496',
      route: 'inkling · v4-flash',
      kind: 'entry',
      t: '—',
      reason: 'entry route — channel default; answered in place, no summon',
      status: 'sealed',
      tone: 'sage',
    },
  ],
  ledger: [],
  history: [],
  ctxK: 82,
  usage: { chip: 'Σ in 8.1k · cache 5.0k (62%) · out 1.6k', tok: '1.1k↑ 0.3k↓' },
}

/* ---------------------------------------------------------------------------
 * Control plane
 * ------------------------------------------------------------------------- */

export const controlData: ControlData = {
  agents: [
    {
      name: 'Inkling',
      tag: 'core · relay',
      tagTone: 'accent',
      state: 'running',
      stateTone: 'accent',
      miniNote: 'verbs: scry · summon · teleport · send · commission | spill >10k · 6h',
      models: [
        { name: 'deepseek : v4-flash', efforts: ['low', 'medium'] },
        { name: 'deepseek : v4-pro', efforts: ['low', 'medium', 'high', 'xhigh'] },
      ],
    },
    {
      name: 'Claude',
      tag: 'opt-in · sdk',
      tagTone: 'teal',
      state: 'idle',
      stateTone: 'sage',
      miniNote: 'ssh atlas · cwd ~/work/relay · hooks /hooks/claude · waits on approvals',
      models: [
        { name: 'opus[1m]', efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'] },
        { name: 'sonnet[1m]', efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'] },
        { name: 'haiku', efforts: ['minimal', 'low', 'medium'] },
      ],
    },
    {
      name: 'Codex',
      tag: 'opt-in · sdk',
      tagTone: 'teal',
      state: '2 warm',
      stateTone: 'sage',
      miniNote: 'sandbox workspace_write · approval user · pool max 8 · ttl 600s',
      models: [
        { name: 'gpt-5.6-sol', efforts: ['minimal', 'low', 'medium', 'high', 'xhigh'] },
        { name: 'gpt-5.5-pro', efforts: ['medium', 'high', 'xhigh'] },
        { name: 'gpt-5.3-codex', efforts: ['low', 'medium', 'high'] },
        { name: 'gpt-5.1-codex-mini', efforts: ['minimal', 'low'] },
      ],
    },
  ],
  agentRoutes,
  mcpRows: [
    { key: 'notion', url: 'mcp.notion.com/mcp', prefix: 'nt', status: '● warm 16s' },
    { key: 'exa', url: 'mcp.exa.ai/mcp', prefix: 'exa', status: '◌ connecting' },
    { key: 'context7', url: 'mcp.context7.com/mcp', prefix: 'c7', status: '○ disabled' },
  ],
  integrations: [
    { name: 'GitHub', flow: 'device', line: 'repo · read:org — githubcopilot mcp · gh · 32 LRU' },
    { name: 'Linear', flow: 'pkce', line: 'read · write — mcp.linear.app · linear_me' },
  ],
  connections: [
    { user: 'kalynnka', connector: 'github', state: 'connected', note: 'warm · 2m ago' },
    { user: 'kalynnka', connector: 'linear_me', state: 'connected', note: 'warm · 1h ago' },
    { user: 'mira', connector: 'github', state: 'pending', note: 'code B4C7-91FD' },
    { user: 'dorian', connector: 'github', state: 'cold', note: 'LRU-evicted' },
  ],
  users: [
    { username: 'kalynnka', kind: 'registered', profileLine: 'slack · lark · qq · web', oauth: 'gh✓ ln✓' },
    { username: 'mira', kind: 'registered', profileLine: 'slack · lark', oauth: 'gh◐' },
    { username: 'dorian', kind: 'registered', profileLine: 'slack · qq', oauth: 'gh✓' },
    { username: 'claude@atlas', kind: 'pseudo', profileLine: 'hook-ingested claude turns', oauth: '—' },
    { username: 'codex@atlas', kind: 'pseudo', profileLine: 'hook-ingested codex turns', oauth: '—' },
    { username: 'ou_3d09x', kind: 'observed', profileLine: 'lark — seen in ops group', oauth: '—' },
  ],
  providers: [
    { name: 'deepseek', status: 'key set', tone: 'sage', src: 'env · DEEPSEEK_API_KEY' },
    { name: 'anthropic', status: 'key set', tone: 'sage', src: 'octomate.yaml' },
    { name: 'openai', status: 'key set', tone: 'sage', src: 'env · OPENAI_API_KEY' },
    { name: 'gemini', status: 'key set', tone: 'sage', src: 'env · GOOGLE_API_KEY' },
    { name: 'vertex', status: 'adc', tone: 'teal', src: 'relay-ops · global' },
    { name: 'bedrock', status: 'off', tone: 'ghost', src: 'my-profile · us-east-1' },
  ],
  hookRows: [
    { k: 'hook_secret', v: 'env ✓', tone: 'sage' },
    { k: '/hooks/claude', v: '● listening', tone: 'ink' },
    { k: '/hooks/codex', v: '● listening', tone: 'ink' },
    { k: 'root · claude', v: '~/.claude/projects + ~/work/transcripts', tone: 'ink' },
    { k: 'root · codex', v: '~/.codex/sessions', tone: 'ink' },
  ],
  obsRows: [
    { k: 'service · env', v: 'octomate · development', tone: 'ink' },
    { k: 'send_to_logfire', v: 'off', tone: 'ghost' },
    { k: 'scrub identifiers', v: 'on', tone: 'sage' },
    { k: 'instrument', v: 'pydantic_ai off · httpx off', tone: 'ghost' },
  ],
  stats: [
    { k: 'Agents', v: '03', sub: '12 routes' },
    { k: 'Channels', v: '06', sub: '4 relay · 2 ingest' },
    { k: 'Threads', v: '12', sub: '48 indexed' },
    { k: 'Sessions', v: '148', sub: '96 relay · 52 native' },
  ],
  feed: [
    { t: '13:59', verb: 'Summon', text: 'inkling → claude·opus[1m] · SES-0522 owns THR-0198' },
    { t: '13:51', verb: 'Ingest', text: 'codex rollout → THR-0203 · offsets 0–12,884' },
    { t: '13:47', verb: 'Spill', text: 'ci.read_log 48.2k → spill_01J9FQ · 6h' },
    { t: '13:44', verb: 'Approval', text: 'gh.push approved by @mira · 13s' },
    { t: '12:58', verb: 'Answer', text: 'pulse answered directly in napcat' },
    { t: '09:12', verb: 'Teleport', text: 'SES-0517 · slack → web, session preserved' },
  ],
  verbBars: [
    { label: 'scry', value: 87 },
    { label: 'send', value: 41 },
    { label: 'summon', value: 23 },
    { label: 'commission', value: 12 },
    { label: 'teleport', value: 9 },
  ],
}

export const statusData: StatusData = {
  left: [
    { t: 'relay nominal', dot: 'teal', tip: 'gateway loop healthy' },
    { t: 'hooks ×2', dot: 'teal', tip: '/hooks/claude · /hooks/codex listening' },
    { t: 'ingest 2 tailers', dot: 'teal', tip: 'claude + codex transcripts tailed' },
    { t: 'mcp 1/3 warm', dot: 'gold', tip: 'notion warm · exa connecting · context7 off' },
  ],
  right: [{ t: 'UTC+08', tip: 'ledger clock' }],
}
