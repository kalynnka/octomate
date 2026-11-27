import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { writeFile, readFile, mkdir, symlink } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { setTimeout } from 'node:timers/promises';
const { apply } = await import(pathToFileURL(process.argv[4]).href);
const config = JSON.parse(process.argv[2]),
  root = process.argv[3];
const require = createRequire(config.executable);
const load = (name) =>
  import(pathToFileURL(require.resolve('@deepseek-ai/' + name)).href);
const { Context, Service } = await load('cordis');
const { LocalSubprocessRuntime } = await load('dsh-subprocess-local');
const ctx = new Context();
let passed = 0;
class Policy extends Service {
  constructor(ctx) {
    super(ctx, 'sandboxPolicy');
  }
  defaultMode = 'workspace-write';
  resolve() {
    return { mode: 'workspace-write', workspaceRoot: root };
  }
}
new Policy(ctx);
new LocalSubprocessRuntime(ctx);
for (const capability of ['sandbox', 'shell', 'fs'])
  await apply(ctx, { ...config, capability });
await setTimeout(100);
const policy = { mode: 'workspace-write', workspaceRoot: root };
const sibling = join(dirname(root), 'sibling');
await mkdir(sibling);
await writeFile(join(sibling, 'secret'), 'private');
await symlink(sibling, join(root, 'escape'));
try {
  const target = await ctx.fs.resolve('proof.txt', { cwd: root });
  const first = await ctx.fs.writeText(
    target,
    'proof',
    undefined,
    undefined,
    policy,
  );
  assert.equal(first.operation, 'create');
  passed++;
  assert.equal(await ctx.fs.readText(target), 'proof');
  passed++;
  await ctx.fs.editText(
    target,
    { oldString: 'proof', newString: 'changed', replaceAll: false },
    { version: first.version },
    undefined,
    policy,
  );
  passed++;
  await assert.rejects(
    ctx.fs.writeText(
      target,
      'bad',
      { kind: 'replaceIfVersion', version: first.version },
      undefined,
      policy,
    ),
    { code: 'FS_STALE_VERSION' },
  );
  passed++;
  await assert.rejects(
    ctx.fs.writeText(target, 'bad', undefined, undefined, {
      ...policy,
      mode: 'read-only',
    }),
    { code: 'FS_SANDBOX_DENIED' },
  );
  passed++;
  for (const path of [join(sibling, 'secret'), join(root, 'escape/secret')]) {
    await assert.rejects(async () =>
      ctx.fs.readText(await ctx.fs.resolve(path, { cwd: root })),
    );
    passed++;
  }
  const parts = [];
  for await (const chunk of await ctx.fs.streamText(target)) parts.push(chunk);
  assert.equal(parts.join(''), 'changed');
  passed++;
  assert.equal(
    Buffer.from(
      await ctx.fs.readByteRange(target, { offset: 1, length: 3 }),
    ).toString(),
    'han',
  );
  passed++;
  assert.equal(
    (await ctx.fs.listDir(await ctx.fs.resolve(root, { cwd: root }))).find(
      (x) => x.name === 'proof.txt',
    ).type,
    'file',
  );
  passed++;
  for (const mode of ['read-only', 'workspace-write', 'danger-full-access']) {
    const result = await (
      await ctx.shell.execute(
        ctx.shell.resolve({
          command: `printf inside; cat '${sibling}/secret'`,
          workdir: root,
          sandboxPolicy: { ...policy, mode },
        }),
      )
    ).result();
    assert.equal(result.stdout.text, 'inside');
    assert.notEqual(result.exitCode, 0);
    passed++;
    const write = await (
      await ctx.shell.execute(
        ctx.shell.resolve({
          command: `printf yes > '${root}/${mode}'`,
          workdir: root,
          sandboxPolicy: { ...policy, mode },
        }),
      )
    ).result();
    assert.equal(write.exitCode === 0, mode !== 'read-only');
    passed++;
  }
  const cancelled = AbortSignal.abort();
  await assert.rejects(ctx.fs.readText(target, cancelled), {
    name: 'AbortError',
  });
  passed++;
  const controller = new AbortController();
  const execution = await ctx.shell.execute(
    ctx.shell.resolve({
      command: 'sleep 60 & child=$!; printf "%s" "$child" > cancel.pid; wait',
      workdir: root,
      sandboxPolicy: policy,
      signal: controller.signal,
    }),
  );
  let pid;
  try {
    for (let attempt = 0; attempt < 100 && pid === undefined; attempt++) {
      try {
        const text = await readFile(join(root, 'cancel.pid'), 'utf8');
        if (text) pid = Number(text);
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      await setTimeout(50);
    }
    assert.ok(pid, 'shell descendant did not start');
  } finally {
    controller.abort();
    await execution.result();
  }
  assert.throws(() => process.kill(pid, 0), { code: 'ESRCH' });
  passed++;
  console.log(JSON.stringify({ passed }));
} finally {
  await ctx.fiber.dispose();
}
