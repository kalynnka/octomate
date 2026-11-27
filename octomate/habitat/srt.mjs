import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';

const [sdk, settings, command] = process.argv.slice(1);
const { SandboxManager, SandboxRuntimeConfigSchema } = await import(sdk);
const config = SandboxRuntimeConfigSchema.parse(JSON.parse(await readFile(settings, 'utf8')));

try {
  // The SDK checks deniedDomains before consulting this callback.
  await SandboxManager.initialize(config, async () => true);
  const wrapped = await SandboxManager.wrapWithSandbox(command);
  // Separate pipes prevent stdout's nonblocking flags from changing stdin.
  const child = spawn(wrapped, { shell: true, stdio: ['pipe', 'pipe', 'inherit'] });
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout);
  process.on('SIGTERM', () => child.kill('SIGTERM'));
  process.on('SIGINT', () => child.kill('SIGINT'));
  process.exitCode = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', code => resolve(code ?? 1));
  });
} finally {
  process.stdin.destroy();
  await SandboxManager.reset();
}
