"""An opt-in timer supplies conversation opportunities to the durable Drift queue."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
import hashlib
import json
from zoneinfo import ZoneInfo

from agent.plugin_composition import Context
from plugins.timer.contract import TIMERS, TimerStatus
from plugins.wake.contract import WAKE_CONTACT

from .initiative import Initiative
from .schema import ProducerConfig


async def follow(ctx: Context, config: ProducerConfig, initiative: Initiative) -> None:
    """One new timer after each check; missed intervals are never replayed."""
    while True:
        async with ctx.runtime_scope():
            timer = ctx.require(TIMERS).schedule(datetime.now(UTC) + timedelta(seconds=config.interval_seconds))
            try:
                receipt = await timer.result()
            finally:
                await timer.cleanup()
            if receipt.status != TimerStatus.FIRED:
                continue
            contact = ctx.require(WAKE_CONTACT)()
            now = receipt.settled_at
            zone = ZoneInfo(contact.timezone)
            local = now.astimezone(zone)
            if not config.start_hour <= local.hour < config.end_hour:
                continue
            target = contact.target()
            identity = json.dumps([receipt.timer_id, receipt.deadline.isoformat(), target],
                                  sort_keys=True, separators=(",", ":"))
            proposal_id = "initiative:" + hashlib.sha256(identity.encode()).hexdigest()
            window_end = datetime.combine(local.date(), time.min, tzinfo=zone) + timedelta(hours=config.end_hour)
            valid_until = min(now + timedelta(hours=1), window_end.astimezone(UTC))
            payload = {"kind": "conversation", "purpose": "结合真实近期对话，考虑是否自然地与用户搭话",
                       "valid_until": valid_until.isoformat()}
            await initiative.write(lambda: initiative.store.propose(
                proposal_id, "1", payload, now, initiative_target=target))
