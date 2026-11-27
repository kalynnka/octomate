import { createRequire } from 'node:module';
import { once } from 'node:events';
import { pathToFileURL } from 'node:url';

const require = createRequire(process.argv[2]);
const { Context } = await import(
  pathToFileURL(require.resolve('@deepseek-ai/cordis')).href
);
const { LocalFileSystem } = await import(
  pathToFileURL(require.resolve('@deepseek-ai/dsh-fs-local')).href
);
const fs = new LocalFileSystem(
  new Context(),
  LocalFileSystem.Config({ cwd: process.cwd() }),
);
const methods = new Set([
  'resolve',
  'lstat',
  'stat',
  'readText',
  'streamText',
  'readBytes',
  'readByteRange',
  'listDir',
  'writeText',
  'editText',
  'patchText',
  'deleteFile',
  'moveFile',
]);
async function send(value) {
  if (!process.stdout.write(JSON.stringify(value) + '\n'))
    await once(process.stdout, 'drain');
}
try {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const request = JSON.parse(Buffer.concat(chunks).toString());
  if (!methods.has(request.method) || !Array.isArray(request.args))
    throw new Error('Invalid filesystem request');
  const value = await fs[request.method](
    ...request.args.map((arg) => (arg === null ? undefined : arg)),
  );
  if (request.method === 'streamText') {
    for await (const chunk of value) await send({ value: chunk });
  } else {
    await send(
      value instanceof Uint8Array
        ? { bytes: Buffer.from(value).toString('base64') }
        : { value },
    );
  }
} catch (error) {
  await send({
    error: {
      message: error.message,
      code: error.code?.startsWith('FS_') ? error.code : 'FS_IO_ERROR',
    },
  });
}
