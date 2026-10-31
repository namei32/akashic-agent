"""Drift v4 → v5: add exact-target memory consent and durable receipts."""
from yoyo import step
from agent.migrations.context import current_migration_context
from drift_migrations.helpers.memory_consent_v5 import upgrade

__depends__ = {"20261029_01_initiative_target"}
__transactional__ = False


def apply_change(_connection: object) -> None:
    upgrade(current_migration_context().bundle_data_roots["drift"] / "drift.sqlite3")


steps = [step(apply_change)]
