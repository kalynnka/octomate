const { SandboxManager } = await import(process.argv[2]);
const { errors, warnings } = SandboxManager.checkDependencies();

for (const message of warnings) console.error(message);
for (const message of errors) console.error(message);
process.exitCode = errors.length ? 1 : 0;
