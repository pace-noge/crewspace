"""Safe Markdown rendering for chat message DTOs."""
from __future__ import annotations

import mistune


_markdown = mistune.create_markdown(escape=True)


def render_message_markdown(body: str) -> str:
    """Render standard Markdown while escaping any raw HTML input."""
    rendered = _markdown(body)
    assert isinstance(rendered, str)
    return rendered
