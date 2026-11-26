"""Prompt template loader for MealBot Claude API calls."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"


class PromptNotFoundError(FileNotFoundError):
    """Raised when no template file exists for a prompt name."""

    def __init__(self, name: str, template_path: Path) -> None:
        """Name the prompt and the path that was looked up."""
        super().__init__(
            f"Prompt template not found: {name} (looked in {template_path})"
        )


def load_prompt(name: str, **kwargs: str) -> str:
    """Load a prompt template and format with provided variables.

    Args:
        name: Template name (without .txt extension).
        **kwargs: Variables to substitute into the template.

    Returns:
        Formatted prompt string.

    Raises:
        PromptNotFoundError: If template doesn't exist (a FileNotFoundError).
        KeyError: If a required variable is missing.
    """
    template_path = PROMPTS_DIR / f"{name}.txt"
    if not template_path.exists():
        raise PromptNotFoundError(name, template_path)
    template = template_path.read_text()
    return template.format(**kwargs)
