import asyncio
from datetime import timedelta
from pathlib import Path

from app import logging
from app import settings
from app.context import AbstractContext
from app.models import Model

logger = logging.get_logger(__name__)

_BACKUP_PATTERN = "aid-*.sqlite3"


class MaintenanceOutcome(Model):
    pruned_items: int
    pruned_runs: int
    # None when today's backup already existed.
    backup: Path | None
    removed_backups: int


def _remove_old_backups(directory: Path) -> int:
    backups = sorted(directory.glob(_BACKUP_PATTERN))
    old = backups[: max(0, len(backups) - settings.BACKUPS_KEPT)]

    for path in old:
        path.unlink()

    return len(old)


async def maintain(
    ctx: AbstractContext, *, backup_directory: Path
) -> MaintenanceOutcome:
    """Daily housekeeping, safe to run every hour: prunes old items and run history,
    then backs the database up once a day and drops the oldest backups."""
    now = ctx.clock.now()
    retention = timedelta(days=settings.RETENTION_DAYS)
    pruned_items = await ctx.items.prune_before(now - retention)
    pruned_runs = await ctx.runs.prune_before(now - 2 * retention)
    backup = backup_directory / f"aid-{now:%Y-%m-%d}.sqlite3"

    if await asyncio.to_thread(backup.exists):
        return MaintenanceOutcome(
            pruned_items=pruned_items,
            pruned_runs=pruned_runs,
            backup=None,
            removed_backups=0,
        )

    await ctx.database.backup(backup)
    removed = await asyncio.to_thread(_remove_old_backups, backup_directory)
    logger.info("Database backed up.", extra={"path": str(backup), "removed": removed})

    return MaintenanceOutcome(
        pruned_items=pruned_items,
        pruned_runs=pruned_runs,
        backup=backup,
        removed_backups=removed,
    )
