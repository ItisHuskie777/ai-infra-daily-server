from app import logging
from app.context import AbstractContext
from app.models import SourceItem

logger = logging.get_logger(__name__)

_EMBEDDING_TEXT_LIMIT = 1500


def build_embedding_text(item: SourceItem) -> str:
    return f"{item.title}\n{item.text[:_EMBEDDING_TEXT_LIMIT]}"


async def embed_pending_items(ctx: AbstractContext, *, batch_size: int = 50) -> int:
    """Embeds every stored item without a vector, batch by batch, until none remain."""
    limit = max(1, batch_size)
    seen: set[str] = set()
    embedded = 0
    batches = 0

    while True:
        pending = await ctx.items.list_unembedded(limit=limit)

        if len(pending) == 0:
            break

        if any(item.id in seen for item in pending):
            logger.error(
                "Items did not leave the unembedded set after being stored; stopping.",
                extra={"batch": batches + 1},
            )

            break

        vectors = await ctx.embeddings.embed(
            [build_embedding_text(item) for item in pending]
        )

        await ctx.items.store_embeddings(
            [(item.id, vector) for item, vector in zip(pending, vectors, strict=True)]
        )

        seen.update(item.id for item in pending)
        embedded += len(pending)
        batches += 1

    logger.info(
        "Embedded pending items.", extra={"embedded": embedded, "batches": batches}
    )

    return embedded
