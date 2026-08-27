"""Skill discovery and loading for application startup."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LoadedSkill:
    """A skill loaded from a local SKILL.md file."""

    name: str
    path: Path
    content: str


def load_skills(skills_dir: Path | None = None) -> dict[str, LoadedSkill]:
    """Discover and load skills from ``skills/<name>/SKILL.md``."""
    directory = skills_dir or Path(__file__).resolve().parents[2] / "skills"
    if not directory.is_dir():
        return {}

    loaded = {}
    for skill_file in sorted(directory.glob("*/SKILL.md")):
        if not skill_file.is_file():
            continue
        name = skill_file.parent.name
        loaded[name] = LoadedSkill(
            name=name,
            path=skill_file,
            content=skill_file.read_text(encoding="utf-8"),
        )
    return loaded