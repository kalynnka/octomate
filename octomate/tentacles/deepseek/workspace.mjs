import { spawn } from 'node:child_process';
import { realpath } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, isAbsolute, relative, resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const inject = ['sandboxPolicy', 'subprocess'];

const worker = fileURLToPath(new URL('./workspace-files.mjs', import.meta.url));
const mutations = {
  writeText: 4,
  editText: 4,
  patchText: 4,
  deleteFile: 3,
  moveFile: 4,
};
const signals = {
  stat: 1,
  readText: 1,
  readBytes: 1,
  readByteRange: 2,
  listDir: 1,
  writeText: 3,
  editText: 3,
  patchText: 3,
  deleteFile: 2,
  moveFile: 3,
};

export async function apply(ctx, config) {
  const require = createRequire(config.executable);
  const load = (name) =>
    import(pathToFileURL(require.resolve(`@deepseek-ai/${name}`)).href);
  const [{ FileSystem, FsError }, { SandboxProvider }] = await Promise.all([
    load('dsh-fs'),
    load('dsh-sandbox'),
  ]);
  const installation = dirname(
    dirname(dirname(require.resolve('@deepseek-ai/dsh-fs/package.json'))),
  );
  const inside = (root, path) => {
    const suffix = relative(root, path);
    return (
      suffix === '' ||
      (suffix !== '..' && !suffix.startsWith('../') && !isAbsolute(suffix))
    );
  };
  async function policy(input) {
    const root = await realpath(input.workspaceRoot);
    if (!inside(config.root, root))
      throw new FsError(
        'Workspace is outside the habitat owner',
        'FS_SANDBOX_DENIED',
      );
    if (
      !['read-only', 'workspace-write', 'danger-full-access'].includes(
        input.mode,
      )
    ) {
      throw new Error('Unknown DSH sandbox mode');
    }
    if (
      input.additionalWritableRoots?.some(
        (path) => !inside(root, resolve(path)),
      )
    ) {
      throw new FsError(
        'Additional writable roots exceed this workspace',
        'FS_SANDBOX_DENIED',
      );
    }
    return { ...input, workspaceRoot: root };
  }
  const argv = (scope, command) => [
    ...config.command,
    JSON.stringify({
      cwd: scope.workspaceRoot,
      read_only: scope.mode === 'read-only',
      read_paths: [installation, dirname(process.execPath), dirname(worker)],
    }),
    ...command,
  ];

  if (config.capability === 'sandbox') {
    class WorkspaceSandbox extends SandboxProvider {
      async confine(command, input, signal) {
        signal?.throwIfAborted();
        return {
          argv: argv(await policy(input), command),
          enforcement: 'full',
          denialSignatures: [
            'operation not permitted',
            'permission denied',
            'read-only file system',
          ],
          runnerFailureRules: [
            { fatalSignatures: ['habitat-execution-start:'] },
          ],
        };
      }
    }
    ctx.plugin(WorkspaceSandbox);
    return;
  }
  if (config.capability === 'shell') {
    const { SandboxBashExecutor } = await load('dsh-bash-sandbox');
    class WorkspaceShell extends SandboxBashExecutor {
      constructor(context) {
        super(context, SandboxBashExecutor.Config({ timeoutMs: 60000 }));
      }
      async execute(spec) {
        spec = {
          ...spec,
          env: {
            ...spec.env,
            OCTOMATE_EXECUTE_ENV: JSON.stringify({
              ...spec.env,
              ...spec.dshEnv,
            }),
          },
        };
        if (spec.sandboxPolicy.mode !== 'danger-full-access')
          return super.execute(spec);
        const scope = await policy(spec.sandboxPolicy);
        const execution = await this.executeArgv(
          spec,
          argv(scope, ['bash', '-c', spec.command]),
        );
        const result = execution.result.bind(execution);
        let completed;
        execution.result = () =>
          (completed ??= result().then((value) => ({
            ...value,
            sandbox: { mode: scope.mode, denied: false },
          })));
        return execution;
      }
    }
    ctx.plugin(WorkspaceShell);
    return;
  }
  if (config.capability !== 'fs')
    throw new Error('Unknown DSH workspace capability');

  class WorkspaceFiles extends FileSystem {
    // Keep native version checks and mutations serialized across short-lived workers.
    tail = Promise.resolve();
    children = new Set();
    constructor(context) {
      super(context);
      context.effect(() => async () => {
        for (const child of this.children) child.kill('SIGTERM');
        await this.tail;
      });
    }
    get sandboxMode() {
      return ctx.sandboxPolicy.defaultMode;
    }
    target(root, target) {
      return { ...target, targetKey: JSON.stringify([root, target.targetKey]) };
    }
    unpack(target) {
      const [root, key] = JSON.parse(target.targetKey);
      if (
        typeof root !== 'string' ||
        typeof key !== 'string' ||
        !inside(config.root, root)
      ) {
        throw new FsError('Invalid workspace target', 'FS_SANDBOX_DENIED');
      }
      return { root, target: { ...target, targetKey: key } };
    }
    processPath(target) {
      return this.unpack(target).target.targetKey;
    }
    fileUrl(target) {
      return pathToFileURL(this.processPath(target)).href;
    }
    contains(parent, child) {
      return inside(this.processPath(parent), this.processPath(child));
    }
    processPathFromHostPath() {
      return undefined;
    }
    async *frames(root, method, args, signal, mode = 'read-only') {
      signal?.throwIfAborted();
      const scope = await policy({ mode, workspaceRoot: root });
      const command = argv(scope, [
        process.execPath,
        worker,
        config.executable,
      ]);
      const child = spawn(command[0], command.slice(1), {
        stdio: ['pipe', 'pipe', 'pipe'],
        env: { PATH: process.env.PATH },
        cwd: scope.workspaceRoot,
      });
      this.children.add(child);
      let errors = '';
      child.stderr.setEncoding('utf8').on('data', (chunk) => {
        errors = (errors + chunk).slice(-8192);
      });
      const done = new Promise((accept, reject) => {
        child.once('error', reject);
        child.once('close', (code) =>
          code === 0
            ? accept()
            : reject(
                new FsError(
                  errors || `Workspace worker exited ${code}`,
                  'FS_IO_ERROR',
                ),
              ),
        );
      });
      // Observe rejection even when an operation is cancelled before its first frame.
      done.catch(() => {});
      const abort = () => child.kill('SIGTERM');
      signal?.addEventListener('abort', abort, { once: true });
      if (signal?.aborted) abort();
      child.stdin.on('error', () => {});
      child.stdin.end(JSON.stringify({ method, args }));
      try {
        for await (const line of createInterface({
          input: child.stdout,
          crlfDelay: Infinity,
        })) {
          const frame = JSON.parse(line);
          if (frame.error)
            throw new FsError(frame.error.message, frame.error.code);
          yield frame.bytes === undefined
            ? frame.value
            : Buffer.from(frame.bytes, 'base64');
        }
        await done;
        signal?.throwIfAborted();
      } finally {
        signal?.removeEventListener('abort', abort);
        child.kill('SIGTERM');
        await done.catch(() => {});
        this.children.delete(child);
      }
    }
    async call(root, method, args, signal, mode) {
      let result;
      for await (const value of this.frames(root, method, args, signal, mode))
        result = value;
      return result;
    }
    async resolve(path, opts = {}) {
      const root = opts.cwd ?? ctx.sandboxPolicy.resolve().workspaceRoot;
      return this.target(
        root,
        await this.call(root, 'resolve', [path, { cwd: root }], opts.signal),
      );
    }
    async lstat(path, opts = {}, signal) {
      const root = opts.cwd ?? ctx.sandboxPolicy.resolve().workspaceRoot;
      return this.call(root, 'lstat', [path, { cwd: root }], signal);
    }
    async streamText(target, signal) {
      const value = this.unpack(target);
      return this.frames(value.root, 'streamText', [value.target], signal);
    }
  }
  for (const [method, signalIndex] of Object.entries(signals)) {
    WorkspaceFiles.prototype[method] = async function (...args) {
      const value = this.unpack(args[0]);
      args[0] = value.target;
      const signal = args[signalIndex];
      args[signalIndex] = undefined;
      if (method === 'moveFile') {
        const destination = this.unpack(args[1]);
        if (destination.root !== value.root)
          throw new FsError(
            'Cannot move between workspace scopes',
            'FS_SANDBOX_DENIED',
          );
        args[1] = destination.target;
      }
      const policyIndex = mutations[method];
      const scope =
        policyIndex === undefined
          ? { mode: 'read-only', workspaceRoot: value.root }
          : (args[policyIndex] ?? ctx.sandboxPolicy.resolve());
      if (policyIndex !== undefined && scope.mode === 'read-only') {
        throw new FsError(
          'File access denied under read-only mode',
          'FS_SANDBOX_DENIED',
        );
      }
      if (
        policyIndex !== undefined &&
        (await realpath(scope.workspaceRoot)) !== (await realpath(value.root))
      ) {
        throw new FsError(
          'Mutation policy does not match its workspace',
          'FS_SANDBOX_DENIED',
        );
      }
      await policy(scope);
      const operation = () =>
        this.call(value.root, method, args, signal, scope.mode);
      const pending = this.tail.then(operation);
      this.tail = pending.catch(() => {});
      const result = await pending;
      return method === 'listDir'
        ? result.map((entry) => ({
            ...entry,
            target: this.target(value.root, entry.target),
          }))
        : result;
    };
  }
  ctx.plugin(WorkspaceFiles);
}
