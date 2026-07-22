from __future__ import annotations

import sys


def test_supported_python_environment_collects_openai_agents() -> None:
    """Keep the workflow runtime on the version that supports the pinned SDK."""

    assert (3, 12) <= sys.version_info[:2] < (3, 13)

    # This import traverses the openai-agents generic tool-context definitions
    # that raise ``KeyError: ~TContext`` during collection on Python 3.11.
    from agents import Agent
    from agents.tool_context import ToolContext

    assert Agent is not None
    assert ToolContext is not None
