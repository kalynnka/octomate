# Projects

Choose a **project** when you want an agent to work on a repository or folder and
keep its file changes for later turns. Projects are made available by the person
running your Octomate server.

## Choose a project before starting

In Trunkline, choose the project when sending the first message of a new
conversation. Tell the agent what you want changed and how you want to review it.

> In the website project, update the getting-started guide. Show me the changes
> and any checks you run before committing anything.

In a connected channel, ask the agent which projects are available, then name the
one you want to use. An agent with Octomate's gateway tools can attach the thread
to it. DeepSeek Harness currently needs the project selected in Trunkline before
the task starts.

Only registered users can select projects. If your project is missing, ask the
operator to [add it to the server configuration](../installation/projects.md).

## Choose a branch or starting point

When continuing existing work, name the branch, tag or commit before the agent
begins:

> Use the website project and start from the release branch. Check the broken
> links and prepare a fix for review.

Otherwise the project starts from its default branch. A conversation can be
attached to one project only. Start a new thread for a different project or
starting point.

## Continue the task

Return to the same thread for follow-up changes. Its
[workspace](workspaces.md) keeps the task's files between turns, separate from
other threads working on the same project.

A native session still uses the files where you started your own agent. Selecting
a project in Octomate does not move those files or take over the local session.

## Review and deliver

Ask for a summary of the changes and the checks performed. Review the diff in
Trunkline or your preferred editor before deciding what to keep.

Saving work in Octomate does not publish it to the upstream repository. Ask
explicitly for a branch, push or pull request when you are ready; the agent needs
the corresponding access. For a project based on a folder, copying accepted
changes back to that folder is also a separate step.

Tell the agent when the task is finished so it can
[release its workspace](workspaces.md#finish-the-task).
