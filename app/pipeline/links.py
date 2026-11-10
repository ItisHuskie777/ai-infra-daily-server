from datetime import datetime
from typing import override

from app import logging
from app.context import AbstractContext
from app.models import Feedback
from app.models import FeedbackSignal
from app.models import QuizQuestion
from app.pipeline import feedback
from app.pipeline import feedback_token
from app.pipeline import quiz
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_SIGNALS = frozenset(signal.value for signal in FeedbackSignal)
_QUESTIONS = frozenset(question.value for question in QuizQuestion)


class LinkError(ServiceError):
    """Why a link in the email did nothing; the transport maps each to a page."""

    INVALID = "invalid"
    EXPIRED = "expired"
    UNKNOWN_TARGET = "unknown_target"
    ALREADY_APPLIED = "already_applied"
    FAILED = "failed"

    @override
    def service(self) -> str:
        return "links"


async def _verified_digest(
    ctx: AbstractContext, *, secret: str, token: str, scope: str, now: datetime
) -> OnSuccess[str]:
    claims = feedback_token.verify(secret, token, now=now, scope=scope)

    if claims is feedback_token.FeedbackTokenError.EXPIRED:
        return LinkError.EXPIRED

    if is_error(claims):
        return LinkError.INVALID

    if is_error(await ctx.digests.find_by_id(claims.digest_id)):
        return LinkError.UNKNOWN_TARGET

    return claims.digest_id


async def _targets_exist(
    ctx: AbstractContext,
    event_id: str | None,
    source_id: str | None,
    item_id: str | None,
) -> bool:
    """The feedback table has foreign keys, so unknown ids are rejected up front."""
    if event_id is not None and is_error(await ctx.events.find_by_id(event_id)):
        return False

    if source_id is not None and is_error(await ctx.sources.find_by_id(source_id)):
        return False

    return item_id is None or len(await ctx.items.find_by_ids([item_id])) == 1


async def apply_feedback_link(
    ctx: AbstractContext,
    *,
    secret: str,
    token: str,
    signal: str,
    event_id: str | None,
    source_id: str | None,
    item_id: str | None,
) -> OnSuccess[str]:
    """Applies one "more", "less", "mute" or "pin" link (FR7.2) and returns the
    confirmation shown to the owner."""
    if signal not in _SIGNALS:
        return LinkError.INVALID

    now = ctx.clock.now()
    digest_id = await _verified_digest(
        ctx,
        secret=secret,
        token=token,
        scope=feedback_token.feedback_scope(signal, event_id, source_id, item_id),
        now=now,
    )

    if is_error(digest_id):
        return digest_id

    if not await _targets_exist(ctx, event_id, source_id, item_id):
        return LinkError.UNKNOWN_TARGET

    received = Feedback(
        created_at=now,
        digest_id=digest_id,
        event_id=event_id,
        item_id=item_id,
        source_id=source_id,
        signal=FeedbackSignal(signal),
    )
    outcome = await feedback.apply_feedback(
        ctx, received, await ctx.preferences.interests()
    )

    if outcome is feedback.FeedbackError.ALREADY_APPLIED:
        return LinkError.ALREADY_APPLIED

    if is_error(outcome):
        logger.error("Applying feedback failed.", extra={"error": str(outcome)})

        return LinkError.FAILED

    await ctx.preferences.save_interests(outcome.interests, now=now)
    logger.info("Feedback recorded.", extra={"signal": signal, "event_id": event_id})

    return "Tomorrow's digest will take this into account."


async def apply_quiz_link(
    ctx: AbstractContext, *, secret: str, token: str, question: str, answer: str
) -> OnSuccess[str]:
    """Applies one answer from the questions at the foot of the email."""
    if question not in _QUESTIONS:
        return LinkError.INVALID

    now = ctx.clock.now()
    digest_id = await _verified_digest(
        ctx,
        secret=secret,
        token=token,
        scope=feedback_token.quiz_scope(question, answer),
        now=now,
    )

    if is_error(digest_id):
        return digest_id

    profile = await ctx.preferences.profile()
    interests = await ctx.preferences.interests()
    outcome = await quiz.apply_answer(
        ctx,
        digest_id=digest_id,
        question=QuizQuestion(question),
        answer=answer,
        profile=profile,
        interests=interests,
        now=now,
    )

    match outcome:
        case quiz.QuizError.ALREADY_ANSWERED:
            return LinkError.ALREADY_APPLIED
        case quiz.QuizError.INVALID:
            return LinkError.INVALID

    if is_error(outcome):
        logger.error("Applying a quiz answer failed.", extra={"error": str(outcome)})

        return LinkError.FAILED

    if outcome.profile != profile:
        await ctx.preferences.save_profile(outcome.profile, now=now)

    if outcome.interests != interests:
        await ctx.preferences.save_interests(outcome.interests, now=now)

    return outcome.message
