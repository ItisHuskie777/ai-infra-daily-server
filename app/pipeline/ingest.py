from collections.abc import Sequence

from app import logging
from app.context import AbstractContext
from app.models import SourceItem

logger = logging.get_logger(__name__)


async def store_new_items(
    ctx: AbstractContext, items: Sequence[SourceItem]
) -> list[str]:
    """Stores items not seen before and returns their ids; identity is the id alone
    (FR2.1)."""
    inserted = await ctx.items.insert_new(items, first_seen_at=ctx.clock.now())
    logger.info(
        "Stored new items.",
        extra={"received": len(items), "inserted": len(inserted)},
    )

    return inserted
