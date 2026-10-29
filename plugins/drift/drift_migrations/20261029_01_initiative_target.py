"""Associate new initiative proposals with their exact contact target."""

from yoyo import step
from agent.migrations.context import current_migration_context
from drift_migrations.helpers.initiative_target_v4 import upgrade

__depends__ = {"20261028_01_initiative_control"}
__transactional__ = False


def apply_change(_connection: object) -> None:
    upgrade(current_migration_context().bundle_data_roots["drift"] / "drift.sqlite3")


steps = [step(apply_change)]
