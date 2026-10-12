const { appendFileSync } = require("node:fs");
const { GitHub, Manifest } = require("release-please");
require("./scoped-python.cjs");

async function main() {
  const { GITHUB_REPOSITORY, GITHUB_OUTPUT, GH_TOKEN } = process.env;
  if (!GITHUB_REPOSITORY || !GITHUB_OUTPUT || !GH_TOKEN) {
    throw new Error(
      "GITHUB_REPOSITORY, GITHUB_OUTPUT and GH_TOKEN are required",
    );
  }
  const [owner, repo] = GITHUB_REPOSITORY.split("/");
  const github = await GitHub.create({ owner, repo, token: GH_TOKEN });
  const branch = github.repository.defaultBranch;
  const releaseManifest = await Manifest.fromManifest(github, branch);
  const releases = (await releaseManifest.createReleases()).filter(
    (release) => release !== undefined,
  );
  const output = [`releases_created=${releases.length > 0}`];
  for (const release of releases) {
    const prefix = release.path === "." ? "" : `${release.path}--`;
    output.push(
      `${prefix}tag_name=${release.tagName}`,
      `${prefix}sha=${release.sha}`,
    );
  }
  const prManifest = await Manifest.fromManifest(github, branch);
  const prs = (await prManifest.createPullRequests()).filter(
    (pr) => pr !== undefined,
  );
  output.push(`prs_created=${prs.length > 0}`);
  if (prs.length > 0) output.push(`pr=${JSON.stringify(prs[0])}`);
  appendFileSync(GITHUB_OUTPUT, `${output.join("\n")}\n`);
}

main();
