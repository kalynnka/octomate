"""Check changed Python files without adding unrelated repository cleanup to a PR."""

import subprocess
import sys
from pathlib import Path

base = sys.argv[1]
command = (
    ["git", "ls-files", "-z"]
    if not base.strip("0")
    else ["git", "diff", "--name-only", "--diff-filter=ACMR", "-z", base, "HEAD"]
)
paths = [
    path
    for path in subprocess.check_output(command).decode().split("\0")
    if path.endswith(".py") and Path(path).is_file()
]
if paths:
    subprocess.run(["uvx", "ruff==0.16.4", "check", *paths], check=True)
    subprocess.run(["uvx", "ruff==0.16.4", "format", "--check", *paths], check=True)
    subprocess.run(
        ["uvx", "pyright==1.1.409", "--pythonpath", sys.executable, *paths], check=True
    )
else:
    print("No changed Python files.")
