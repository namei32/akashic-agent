"""Read-only liveness of a background child belonging to the given parent session."""
from collections.abc import Callable

from agent.plugin_composition import ServiceKey
from plugins.jobs.contract import JobPanelSource

SUBAGENT_WAITING = ServiceKey[Callable[[str, str], bool]]("subagent.waiting.v1")
SUBAGENT_PANEL = ServiceKey[JobPanelSource]("subagent.panel.v1")
