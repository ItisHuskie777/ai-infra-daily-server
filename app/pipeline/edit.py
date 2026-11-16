from collections.abc import Sequence

from pydantic import BaseModel
from pydantic import Field

from app import logging
from app.context import AbstractContext
from app.models import DigestEventPlacement
from app.pipeline import _untrusted
from app.result import is_error

logger = logging.get_logger(__name__)

_RECENT_TITLE_LIMIT = 200
_SYSTEM_PROMPT = (
    "You are the editor of a daily AI-news email. You catch duplicate stories before "
    "the email goes out. " + _untrusted.RULE
)


class _Plan(BaseModel):
    same_story: list[list[int]] = Field(default_factory=list)
    repeats: list[int] = Field(default_factory=list)


def _describe(index: int, placement: DigestEventPlacement) -> str:
    items = sorted(
        placement.event.items, key=lambda item: item.published_at, reverse=True
    )
    headlines = " | ".join(_untrusted.fence(item.title) for item in items[:3])

    return f"[{index}] ({placement.section}) {headlines}"


def _build_prompt(
    placements: Sequence[DigestEventPlacement], recent_titles: Sequence[str]
) -> str:
    recent = (
        "\n".join(
            f"- {_untrusted.fence(title)}"
            for title in recent_titles[:_RECENT_TITLE_LIMIT]
        )
        or "(none)"
    )

    return "\n".join(
        [
            "Today's candidate stories, numbered, with up to three headlines each:",
            *(
                _describe(index, placement)
                for index, placement in enumerate(placements)
            ),
            "",
            "Headlines already sent in the last few days:",
            recent,
            "",
            "Return JSON with two lists:",
            '- "same_story": groups of candidate numbers that cover one story, for '
            "example the same announcement in English and in Chinese, or several "
            "reactions to one event. Group only when you are confident.",
            '- "repeats": candidate numbers that only repeat a story already sent, '
            "with no new development since.",
        ]
    )


def _merged(
    keep: DigestEventPlacement, others: Sequence[DigestEventPlacement]
) -> DigestEventPlacement:
    items = {item.id: item for item in keep.event.items}
    item_ids = list(keep.item_ids)

    for other in others:
        items.update({item.id: item for item in other.event.items})
        item_ids.extend(
            item_id for item_id in other.item_ids if item_id not in item_ids
        )

    return keep.model_copy(
        update={
            "event": keep.event.model_copy(update={"items": tuple(items.values())}),
            "item_ids": tuple(item_ids),
        }
    )


def apply_plan(
    placements: Sequence[DigestEventPlacement],
    same_story: Sequence[Sequence[int]],
    repeats: Sequence[int],
) -> list[DigestEventPlacement]:
    """Pure: each group collapses into its highest-scoring member, which takes the
    others' items, and repeats are dropped. Out-of-range or reused numbers are
    ignored, so a sloppy reply cannot remove the wrong story."""
    valid = range(len(placements))
    merged: dict[int, DigestEventPlacement] = {}
    dropped: set[int] = set()

    for group in same_story:
        members = sorted(
            {index for index in group if index in valid and index not in dropped},
            key=lambda index: -placements[index].event.relevance_score,
        )

        if len(members) < 2 or any(index in merged for index in members):
            continue

        keep, *rest = members
        merged[keep] = _merged(placements[keep], [placements[i] for i in rest])
        dropped.update(rest)

    dropped.update(index for index in repeats if index in valid and index not in merged)

    return [
        merged.get(index, placement)
        for index, placement in enumerate(placements)
        if index not in dropped
    ]


async def review(
    ctx: AbstractContext,
    placements: Sequence[DigestEventPlacement],
    *,
    recent_titles: Sequence[str],
) -> list[DigestEventPlacement]:
    """One model call over the whole selection: stories reported several times today
    become one entry, and stories that only repeat recent days are left out. When the
    call fails the selection goes out unchanged."""
    if len(placements) < 2:
        return list(placements)

    plan = await ctx.llm.complete_json(
        system=_SYSTEM_PROMPT,
        user=_build_prompt(placements, recent_titles),
        schema=_Plan,
        temperature=0.0,
    )

    if is_error(plan):
        logger.warning(
            "Editor review failed; keeping the selection.", extra={"error": str(plan)}
        )

        return list(placements)

    reviewed = apply_plan(placements, plan.same_story, plan.repeats)
    logger.info(
        "Editor review applied.",
        extra={
            "candidates": len(placements),
            "kept": len(reviewed),
            "groups": len(plan.same_story),
            "repeats": len(plan.repeats),
        },
    )

    return reviewed
