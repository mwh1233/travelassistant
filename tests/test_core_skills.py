from pathlib import Path

from app.core.skills import load_skills


def test_load_skills_reads_skill_documents(tmp_path: Path):
    first = tmp_path / "z-last" / "SKILL.md"
    second = tmp_path / "a-first" / "SKILL.md"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text("last", encoding="utf-8")
    second.write_text("first", encoding="utf-8")
    (tmp_path / "ignored.md").write_text("ignored", encoding="utf-8")

    skills = load_skills(tmp_path)

    assert list(skills) == ["a-first", "z-last"]
    assert skills["a-first"].content == "first"
    assert skills["a-first"].path == second


def test_load_skills_returns_empty_for_missing_directory(tmp_path: Path):
    assert load_skills(tmp_path / "missing") == {}