import asyncio
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime

from app import logging
from app import settings
from app.context import AbstractContext
from app.models import Model
from app.models import Source
from app.models import SourceHealth
from app.models import SourceItem
from app.repositories import SourceRun
from app.result import is_error
from app.sources._common import FetchError
from app.sources._common import ImplementsSourceAdapter

logger = logging.get_logger(__name__)


class FetchOutcome(Model):
    items: tuple[SourceItem, ...]
    health: tuple[SourceHealth, ...]


class _Settled(Model):
    items: tuple[SourceItem, ...] = ()
    error: str | None = None
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _newest_first(items: Sequence[SourceItem]) -> list[SourceItem]:
    return sorted(items, key=lambda item: item.published_at, reverse=True)


async def _fetch_one(
    ctx: AbstractContext,
    source: Source,
    adapters: Mapping[str, ImplementsSourceAdapter],
    semaphore: asyncio.Semaphore,
) -> _Settled:
    adapter = adapters.get(source.kind)

    if adapter is None:
        return _Settled(error=str(FetchError.UNSUPPORTED), detail=source.kind)

    async with semaphore:
        # NOTE: Adapter boundary; a vendor exception becomes a value so siblings keep
        # running (FR1.5).
        try:
            async with asyncio.timeout(settings.SOURCE_DEADLINE_SECONDS):
                fetched = await adapter.fetch(ctx, source)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "Source fetch hit its deadline.", extra={"source": source.id}
            )

            return _Settled(error=str(FetchError.FETCH_FAILED), detail="deadline")
        except Exception as cause:
            logger.exception("Source adapter raised.", extra={"source": source.id})

            return _Settled(error=str(FetchError.FETCH_FAILED), detail=str(cause))

    if is_error(fetched):
        return _Settled(error=str(fetched))

    return _Settled(
        items=tuple(_newest_first(fetched)[: settings.MAX_ITEMS_PER_SOURCE])
    )


async def _record_run(
    ctx: AbstractContext, source: Source, settled: _Settled, started_at: datetime
) -> SourceHealth:
    await ctx.sources.record_run(
        SourceRun(
            source_id=source.id,
            started_at=started_at,
            finished_at=ctx.clock.now(),
            ok=settled.ok,
            error=settled.error,
            detail=settled.detail,
            item_count=len(settled.items),
        )
    )

    return SourceHealth(
        source_id=source.id,
        name=source.name,
        ok=settled.ok,
        error=settled.error,
        item_count=len(settled.items),
    )


async def fetch_all(
    ctx: AbstractContext,
    sources: Sequence[Source],
    *,
    adapters: Mapping[str, ImplementsSourceAdapter],
) -> FetchOutcome:
    """Fetches every enabled source; a failure is recorded and reported in source order,
    never propagated (FR1.5). Items are deduplicated by id across sources."""
    enabled = [source for source in sources if source.enabled]
    started_at = ctx.clock.now()
    semaphore = asyncio.Semaphore(settings.SOURCE_FETCH_CONCURRENCY)
    settled = await asyncio.gather(
        *(_fetch_one(ctx, source, adapters, semaphore) for source in enabled)
    )
    seen: set[str] = set()
    items: list[SourceItem] = []
    health: list[SourceHealth] = []

    for source, result in zip(enabled, settled, strict=True):
        if result.ok:
            logger.info(
                "Fetched source.",
                extra={"source": source.id, "count": len(result.items)},
            )
        else:
            logger.error(
                "Source fetch failed.",
                extra={
                    "source": source.id,
                    "error": result.error,
                    "detail": result.detail,
                },
            )

        health.append(await _record_run(ctx, source, result, started_at))

        for item in result.items:
            if item.id not in seen:
                seen.add(item.id)
                items.append(item)

    return FetchOutcome(items=tuple(items), health=tuple(health))
