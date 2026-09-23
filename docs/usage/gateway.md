# Moving a conversation

The **gateway** is the set of tools an agent uses to decide where a conversation
goes and who handles it. Octomate calls them spells. The default is for the agent
to do the work itself; a spell is for the cases below. Every agent gets the same
instructions and the same refusals, so they all learn from one wording.

| Spell | What moves | Who continues |
|---|---|---|
| `scry` | Nothing. Reveals routes, destinations or projects. | |
| `summon` | The conversation, to another agent | That agent, stickily |
| `teleport` | The same agent, to a new sub-thread or into a project | The same agent |
| `scheme` | The work, into the asking user's direct messages | Whoever already handles their DMs |
| `send` | A message, now, without ending the turn | The same agent |
| `dispel` | Nothing. Releases the thread's workspace when the turn ends. | |

Over MCP the names carry a `gateway_` prefix. Inkling adds `commission` and
`whisper`, described at the end.

## scry

`scry` takes one facet and answers with lines the agent copies from:

- `routes`: the agents on this channel other than itself, each with its model and
  claim. A summon names a route exactly as listed.
- `destinations`: everywhere else the person can be reached privately, with the
  agents that run there. Requires the person to have linked profiles on other
  channels.
- `projects`: the declared projects by name and description, never their paths.
  Only for a registered user.

One facet per call, because a per-user list is the one thing that must never end
up in a cached prompt prefix.

## summon

A summon is a real hand-off: the other agent takes over this turn and its
follow-ups. The bar is high. The instruction tells the agent to summon only when
the request needs a capability it lacks, such as running code in a real repository,
or when it is substantial specialist work another agent would do markedly better.
Length or a technical topic is not a reason, and a guess is never one.

A summon names the route, a `destination`, a user-facing `hint`, a one-line
`reason` that is recorded but not shown, and a self-contained **brief** of up to
eight thousand characters. The receiver may not see this chat, so the brief carries
the goal, the decisions, what was tried, and what done looks like, citing ledger
messages by handle rather than pasting them. A brief over the cap is refused, never
trimmed. `effort` is set only when the user asked for a level.

Destinations:

- `here`: take over this conversation in place. Not offered on a group's main
  surface, where it would pin an owner for everyone.
- `thread`: open a sub-thread of this chat. Not offered from inside a thread, since
  threads do not nest, nor on a platform that opens none.
- a channel id from `scry`: open a thread in the person's direct messages on that
  channel, for work that belongs where they actually do it. The route is validated
  against that channel's own agents.

## teleport

The same agent continues somewhere else with everything said so far, and the turn
ends on it. Two uses:

- **A thread of its own.** Multi-step or long-running work that deserves a thread
  but that this agent is the right one to do. Default `destination` is `thread`.
- **A project.** `project` binds the thread the agent lands in, and `ref` picks a
  branch, tag or commit to start from. From inside a thread, `destination: here`
  binds this thread without moving. The instruction says to do it before starting
  work, because a thread about no project runs in a throwaway tree.

A crossing into another channel's DMs is offered only from a conversation nobody
else can read, because everything said travels with it. A native session cannot be
teleported at all; its work is wherever its terminal is.

## scheme

Take it to the person privately. The agent writes a `hint`, the first line they read
in their DM, and a `brief` for whoever answers there. The receiver is whoever
already owns that person's direct messages, or the channel's default agent, never
one this run chose: a group can never point someone's private assistant at an agent
of its choosing. Nothing is posted in the origin, so the agent closes its own reply
by saying the work is moving. Only from a group, on a platform with DMs.

## send

Deliver something now: a progress update, an intermediate result, an image or a
file. Anything sent is already delivered, so the final reply continues from it
rather than restating it. `destination: dm` sends privately to the asker without
handing anything over. A reply segment first in the list threads the message onto a
specific ledger message by its handle; an `at` segment pings a user.

## dispel

A project thread keeps its workspace between turns. When the work is done for good,
merged, delivered or dropped, `dispel` releases it when the turn ends, after the
turn's work is saved to the mirror. Not for a pause. Refused for a thread in no
project and for a native session.

## commission and whisper

Inkling only. A `commission` puts another agent to work in the background on a
self-contained brief and returns its report as the tool result; the user sees only
Inkling's reply. The accomplice has no user to ask, so it declines every approval
and proceeds on stated assumptions, and it is bounded to fifteen minutes. `whisper`
follows up with an accomplice by the name it was given, keeping its context.
Several commissions in one reply run concurrently. The harness-driven agents bring
their own subagents and are not offered these.

## From a native session

Over MCP a native session speaks for its user with no thread of its own, so every
destination is a crossing. It can `scry`, `summon` and `scheme` into a real
channel, which kicks a new turn there, and `send` to a named destination. It cannot
teleport or dispel.
