# Workspaces

A workspace is the agent's working copy for a project task. Each project thread
has its own, so you can ask different agents to work on separate tasks without
having them edit the same working files.

## Keep file work in a project

[Choose a project](projects.md) before asking an agent to create or change files.
Continue in the same thread when you want it to build on those changes.

> Update the draft we worked on yesterday, keeping the examples we agreed to use.

A conversation without a project is suitable for discussion, but files created
there are temporary and are not kept between turns. Inkling has no file or shell
tools until a project is selected.

## Resume where you left off

Project work is saved after each turn. You can leave the conversation and return
later to continue from its saved files. After a long pause, preparing the workspace
again may take longer, especially if dependencies need to be installed.

Keep important deliverables in the project's ordinary files. Temporary or ignored
files, such as a local dependency environment, may need to be recreated. If the
agent reports a save failure, resolve it before treating the work as safely stored.

## Review the result

Open the thread in Trunkline to inspect its workspace changes, or ask the agent
for a diff and a summary:

> Show the files you changed, explain the result, and list the checks that passed
> or still need to be run.

Octomate saving the workspace is separate from the agent making a commit or
publishing changes. Ask for those steps explicitly when you are ready.

The agent's [permission mode](agents/permissions.md) controls what it may do.
A separate working copy is useful for keeping tasks apart, but it does not mean
every agent is restricted to that directory under every mode.

## Finish the task

When you have accepted and delivered the result, tell the agent the work is done:

> The changes are merged. Release this task's workspace.

An agent with the gateway tools can release the working copy after saving the
turn's work. This does not erase the conversation. For a normal pause, simply
leave the thread and return later; you do not need to release it yourself.
