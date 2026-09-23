# Approvals and questions

Most tools give you one global switch. Octomate raises **actions** instead. One
action is exactly one thing you are asked: one approval for one tool call, or one
question. Actions arrive as a **batch**, everything a turn is waiting on together,
so a turn that needs two tools and an answer interrupts you once.

## What an action is

| Kind | Raised when | You answer with |
|---|---|---|
| Approval | A tool needs permission under the conversation's posture | Approve or deny, optionally "allow for session" |
| Question | The agent asks something, with up to three choices and a hint | A choice, or free text |

Questions are capped at three choices for consistent cards; a runtime whose native
tool offers more is trimmed, and free text is always accepted. Each action carries
who answered it and when, which is what makes "who approved that" a row.

## What happens while you decide

The run is suspended and the batch is persisted with everything needed to resume
it. How the wait works differs by agent:

- **Inkling** ends the run on the deferral. The batch lives in the database, and
  the answer resumes the same conversation through the graph, even after a restart.
- **Claude Code, Codex and DeepSeek Harness** keep the live process parked while
  the card waits, because the harness itself is blocked on the answer. A restart
  loses the parked run, though the conversation resumes on the next message.

An unanswered card expires after the agent's `approval_timeout`, one hour by
default. The tool is then denied with a message saying so, and the run finishes. A
run with no user, a commissioned accomplice, is denied immediately.

"Allow for session" adds the tool to the conversation's allow list, persisted, so
the next turn does not ask again. Trunkline can change a conversation's permission
mode outright.

## Where cards appear

Cards are posted on the surface the turn is answering, sub-thread included. Each
channel renders them its own way; see the
[render matrix](channels/index.md#what-each-channel-renders). Slack and Lark carry
the batch's state in the buttons; Discord carries ids and reloads the action from
the database; Trunkline shows the batch in the thread and resolves it over the API.
Any of them can answer a batch raised on another channel's thread through
Trunkline. On QQ, cards are text and cannot be answered.

A batch is resumed exactly once. Pressing a button on an already resolved batch is
refused, not replayed.

## Postures

Which actions arise at all is the runtime's posture, chosen per agent in
[configuration](agents/permissions.md#permission-modes) and switchable per conversation
from Trunkline. Inkling's `dontAsk` and `bypassPermissions` resolve questions and
approvals in process and say in the report what they assumed; Codex's
`auto_review` and `full_access` never raise a request; Claude's SDK scale is
handed over as it is.
