"""Read-only contact selected by the active Wake function."""
from collections.abc import Callable
from dataclasses import dataclass

from agent.plugin_composition import ServiceKey


@dataclass(frozen=True, slots=True)
class WakeContact:
    channel: str
    recipient: str
    session_id: str
    timezone: str

    def target(self) -> dict[str, str]:
        return {"channel": self.channel, "recipient": self.recipient, "session_id": self.session_id}


WAKE_CONTACT = ServiceKey[Callable[[], WakeContact]]("wake.contact.v1")
# Published only when Wake can safely consume memory-assisted opportunities.
WAKE_MEMORY_READY = ServiceKey[object]("wake.memory-initiative.v1")
