const { registerReleaseType } = require("release-please");
const { Python } = require("release-please/build/src/strategies/python");

class ScopedPython extends Python {
  /** @param {import("release-please").ConventionalCommit[]} commits */
  async postProcessCommits(commits) {
    return commits.map((commit) => {
      const paths = (commit.scope || "core").split(",").map((scope) => {
        const name = scope.trim();
        return name === "cli" || name === "protocol" ? name : ".";
      });
      if (paths.some((path) => path === this.path)) return commit;
      return {
        ...commit,
        breaking: false,
        message: commit.message.replace(/^(\w+(?:\([^)]+\))?)!:/, "$1:"),
        notes: commit.notes.filter(
          (note) =>
            note.title !== "BREAKING CHANGE" && note.title !== "RELEASE AS",
        ),
      };
    });
  }
}

registerReleaseType("scoped-python", (options) => new ScopedPython(options));
