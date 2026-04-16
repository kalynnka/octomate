"""Package a skill directory into a distributable .skill zip file.

Usage: python package_skill.py <path/to/skill-folder>

Creates <skill-name>.skill in the parent directory of the skill folder.
"""

import sys
import zipfile
from pathlib import Path


def package_skill(skill_path: Path) -> Path:
    if not skill_path.is_dir():
        raise ValueError(f"Not a directory: {skill_path}")

    skill_md = skill_path / "SKILL.md"
    if not skill_md.exists():
        raise ValueError(f"No SKILL.md found in {skill_path}")

    output_path = skill_path.parent / f"{skill_path.name}.skill"

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(skill_path.rglob("*")):
            if file.is_file() and not any(
                part.startswith(".") or part == "__pycache__"
                for part in file.parts
            ):
                zf.write(file, file.relative_to(skill_path.parent))

    return output_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python package_skill.py <path/to/skill-folder>")
        sys.exit(1)

    skill_path = Path(sys.argv[1]).resolve()
    try:
        output = package_skill(skill_path)
        print(f"Packaged: {output}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
