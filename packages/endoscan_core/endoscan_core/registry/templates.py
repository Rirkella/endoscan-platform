"""Load and render the registry's card/config templates.

Templates use ``{{ token }}`` placeholders. M1 ships the template set so later
milestones (e.g. the M3 ER model card) render real content from one canonical
source. Rendering is intentionally trivial string substitution — no templating
engine dependency.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_PLACEHOLDER = re.compile(r"{{\s*(\w+)\s*}}")


def template_path(name: str) -> Path:
    """Return the path to a named template, or raise ``FileNotFoundError``."""
    path = _TEMPLATES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"Registry template not found: {name}")
    return path


def load_template(name: str) -> str:
    """Return the raw text of a named template."""
    return template_path(name).read_text(encoding="utf-8")


def template_placeholders(name: str) -> set[str]:
    """Return the set of ``{{ token }}`` placeholder names used in a template."""
    return set(_PLACEHOLDER.findall(load_template(name)))


def render_template(name: str, values: dict[str, str]) -> str:
    """Render a template, substituting ``{{ token }}`` with ``values``.

    Tokens missing from ``values`` are left untouched so callers can render in
    passes or detect unfilled fields.
    """
    text = load_template(name)

    def _substitute(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER.sub(_substitute, text)
