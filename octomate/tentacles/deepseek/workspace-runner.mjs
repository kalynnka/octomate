import { spawn } from 'node:child_process';
import { mkdtemp, mkdir, readFile, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const [rawConfig, rawScope, ...argv] = process.argv.slice(2);
const config = JSON.parse(rawConfig);
const scope = JSON.parse(rawScope);
const { SandboxManager, SandboxRuntimeConfigSchema } = await import(
  pathToFileURL(config.runtime).href
);
const temp = await mkdtemp('/tmp/octo-dsh-');
let child;
const cancellation = new AbortController();
const terminate = () => {
  cancellation.abort();
  if (child?.pid === undefined) return;
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
};
process.once('SIGTERM', terminate);
process.once('SIGINT', terminate);
try {
  const settings = JSON.parse(await readFile(config.settings, 'utf8'));
  const literal = (path) => ({ path, literal: true });
  settings.filesystem.allowRead = settings.filesystem.allowRead.filter(
    (entry) => entry.path !== config.root,
  );
  settings.filesystem.allowRead.push(
    ...[
      scope.workspaceRoot,
      temp,
      config.installation,
      dirname(config.node),
      dirname(fileURLToPath(import.meta.url)),
    ].map(literal),
  );
  settings.filesystem.allowWrite = [literal(temp)];
  if (scope.mode !== 'read-only')
    settings.filesystem.allowWrite.push(literal(scope.workspaceRoot));
  await mkdir(join(temp, 'home'));
  await SandboxManager.initialize(
    SandboxRuntimeConfigSchema.parse(settings),
    async () => true,
  );
  cancellation.signal.throwIfAborted();
  const quote = (value) => `'${value.replaceAll("'", "'\\''")}'`;
  const command = argv.map(quote).join(' ');
  const wrapped = await SandboxManager.wrapWithSandbox(
    command,
    undefined,
    undefined,
    cancellation.signal,
  );
  child = spawn(wrapped, {
    shell: true,
    stdio: ['pipe', 'pipe', 'inherit'],
    detached: true,
    env: {
      PATH: process.env.PATH,
      ...JSON.parse(process.env.OCTOMATE_EXECUTE_ENV ?? '{}'),
      HOME: join(temp, 'home'),
      TMPDIR: temp,
    },
  });
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout);
  process.exitCode = await new Promise((accept, reject) => {
    child.once('error', reject);
    child.once('close', (code) => accept(code ?? 1));
  });
  terminate();
} catch (error) {
  console.error(`octomate-srt-start: ${error.message}`);
  process.exitCode = 1;
} finally {
  terminate();
  process.stdin.destroy();
  await SandboxManager.reset();
  await rm(temp, { recursive: true, force: true });
}
