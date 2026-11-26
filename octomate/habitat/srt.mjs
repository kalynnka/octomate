import { spawn } from 'node:child_process';
import { readFile } from 'node:fs/promises';

const [sdk, settings, command] = process.argv.slice(1);
const { SandboxManager, SandboxRuntimeConfigSchema } = await import(sdk);
const config = SandboxRuntimeConfigSchema.parse(JSON.parse(await readFile(settings, 'utf8')));

try {
  // The SDK checks deniedDomains before consulting this callback.
  await SandboxManager.initialize(config, async () => true);
  const wrapped = await SandboxManager.wrapWithSandbox(command);
  const child = spawn(wrapped, { shell: true, stdio: 'inherit' });
  process.on('SIGTERM', () => child.kill('SIGTERM'));
  process.on('SIGINT', () => child.kill('SIGINT'));
  process.exitCode = await new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', code => resolve(code ?? 1));
  });
} finally {
  await SandboxManager.reset();
}
