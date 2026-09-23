# Approvals and questions

When an agent needs permission or a decision, answer the request in the
conversation or open it in Trunkline. A task may wait for several answers before
it continues.

## Approve or deny a tool call

1. Read what the agent wants to do and check the files, command or service involved.
2. Choose **Approve** to allow that request or **Deny** to refuse it.
3. If offered, choose **Allow for session** only when you want later uses of that
   tool in the same conversation to proceed without asking again.

Denying a request lets the agent know it cannot take that action. You can follow
up with an alternative, such as asking it to prepare changes for review.

## Answer a question

Select a suggested answer or enter your own. If the card contains several
questions, complete them and submit the answers. Typing into an unfinished card
alone does not send your decision.

> Use the shorter version, keep the examples, and leave the introduction as it is.

Specific answers help the agent continue without another round of clarification.

## Find a pending request { #where-cards-appear }

Look in the thread where the task is running. You can also open that thread in
Trunkline to answer its pending requests, even when the conversation began in
another channel. The exact buttons and layout vary by channel.

If you have already answered, check the conversation for progress before trying
again. The same request cannot be resolved twice.

## If the request expires or the server restarts

Unanswered requests usually expire after an hour, unless the operator has changed
the timeout. The pending tool call is denied; return to the conversation and tell
the agent whether to try again or take a different approach.

After a server restart, Claude Code, Codex and DeepSeek Harness may need a fresh
message to continue interrupted work. Inkling can resume its saved questions and
approvals. Check the result before repeating an action that might already have
completed.

## Change how often the agent asks

Use the conversation's permission controls in Trunkline. A mode that allows more
actions automatically will show fewer approval requests. Read
[Permission modes](agents/permissions.md) before changing it; the available
choices depend on the agent.
