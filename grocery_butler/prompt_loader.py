"""Prompt template loader for MealBot Claude API calls."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and format with provided variables.

    Args:
        name: Template name (without .txt extension).
        **kwargs: Variables to substitute into the template.

    Returns:
        Formatted prompt string.

    Raises:
        FileNotFoundError: If template doesn't exist.
        KeyError: If a required variable is missing.
    """
    template_path = PROMPTS_DIR / f"{name}.txt"
    if not template_path.exists():
        raise FileNotFoundError(
            f"Prompt template not found: {name} (looked in {template_path})"
        )
    template = template_path.read_text()
    return template.format(**kwargs)
