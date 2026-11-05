from collections.abc import Sequence
from typing import override

from pydantic import BaseModel

from app import logging
from app.context import AbstractContext
from app.models import Comment
from app.models import StoredItem
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_CANDIDATE_MULTIPLIER = 3
_MAX_CANDIDATES = 20
_COMMENT_TEXT_LIMIT = 500
_KEEP_SYSTEM_PROMPT = (
    "You curate reader comments for a news digest. Keep only comments that add a "
    "distinct viewpoint, first-hand experience or a substantive correction; drop "
    "agreement, jokes and restatements."
)


class CommentSelectionError(ServiceError):
    INVALID_LIMIT = "invalid_limit"

    @override
    def service(self) -> str:
        return "comments"


class _Keep(BaseModel):
    keep: list[str]


def _score_order(comment: Comment) -> tuple[int, str]:
    return (-(comment.score or 0), comment.id)


def _build_keep_prompt(
    item: StoredItem, candidates: Sequence[Comment], limit: int
) -> str:
    lines = [f'Post: "{item.title}"', "", "Comments, highest engagement first:"]

    for comment in candidates:
        author = "" if comment.author is None else f" by {comment.author}"
        text = comment.text[:_COMMENT_TEXT_LIMIT].replace("\n", " ")
        lines.append(f"- id {comment.id}{author} (score {comment.score or 0}): {text}")

    lines.append("")
    lines.append(
        f"Return the ids of at most {limit} comments worth keeping, in order of value."
    )

    return "\n".join(lines)


async def select_comments(
    ctx: AbstractContext, item: StoredItem, *, limit: int
) -> OnSuccess[list[Comment]]:
    """Top comments by engagement, filtered by the model for distinct viewpoints
    (FR5.3); a model failure falls back to engagement order."""
    if limit < 1:
        return CommentSelectionError.INVALID_LIMIT

    if len(item.comments) == 0:
        return []

    ranked = sorted(item.comments, key=_score_order)
    candidates = ranked[: min(_MAX_CANDIDATES, limit * _CANDIDATE_MULTIPLIER)]
    fallback = ranked[:limit]

    if len(candidates) <= 1:
        return fallback

    generated = await ctx.llm.complete_json(
        system=_KEEP_SYSTEM_PROMPT,
        user=_build_keep_prompt(item, candidates, limit),
        schema=_Keep,
        temperature=0.0,
    )

    if is_error(generated):
        logger.warning(
            "Comment selection failed; falling back to engagement order.",
            extra={"item_id": item.id, "error": str(generated)},
        )

        return fallback

    by_id = {comment.id: comment for comment in candidates}
    kept: list[Comment] = []

    for comment_id in generated.keep:
        comment = by_id.get(comment_id)

        if comment is not None and comment not in kept:
            kept.append(comment)

    if len(kept) == 0:
        logger.warning(
            "Model kept no known comment; falling back to engagement order.",
            extra={"item_id": item.id},
        )

        return fallback

    return kept[:limit]
