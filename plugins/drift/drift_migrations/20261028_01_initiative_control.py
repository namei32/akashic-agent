"""Drift v2 → v3: append durable initiative control and operation receipts."""

from yoyo import step
from agent.migrations.context import current_migration_context
from drift_migrations.helpers.initiative_control_v3 import upgrade

__depends__: set[str] = set()
__transactional__ = False


def apply_change(_connection: object) -> None:
    upgrade(current_migration_context().bundle_data_roots["drift"] / "drift.sqlite3")


steps = [step(apply_change)]
