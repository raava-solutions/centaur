"""Safety and discovery tests for the outreach-operator persona."""

from pathlib import Path
import tomllib


PERSONA_DIR = Path(__file__).resolve().parent


def test_pyproject_declares_centaur_persona():
    data = tomllib.loads((PERSONA_DIR / "pyproject.toml").read_text())

    assert data["project"]["name"] == "outreach-operator"
    assert data["tool"]["centaur"]["type"] == "persona"
    assert data["tool"]["centaur"]["engine"] == "codex"
    assert data["tool"]["centaur"]["prompt"] == "PROMPT.md"


def test_prompt_exists():
    assert (PERSONA_DIR / "PROMPT.md").is_file()


def test_prompt_locks_send_rule():
    prompt = (PERSONA_DIR / "PROMPT.md").read_text().lower()

    assert "explicit" in prompt
    assert "echo" in prompt
    assert "confirm" in prompt
    assert "never auto-send" in prompt
    assert "ambiguous" in prompt
    assert "never send on your own initiative" in prompt
