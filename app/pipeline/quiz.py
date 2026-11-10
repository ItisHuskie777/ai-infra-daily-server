from collections import Counter
from collections.abc import Sequence
from datetime import date as date_type
from datetime import datetime
from typing import override

from app import logging
from app import settings
from app.context import AbstractContext
from app.models import DigestEventPlacement
from app.models import DigestStyle
from app.models import Interests
from app.models import Model
from app.models import Profile
from app.models import QuizQuestion
from app.models import TechnicalDepth
from app.models import Topic
from app.pipeline import recommend
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_TOPIC_OPTION_LIMIT = 4
_MIN_TOPIC_OPTIONS = 2
_LESS = "less"
_SAME = "same"
_MORE = "more"
_STEPS = {_LESS: -1, _SAME: 0, _MORE: 1}
_STYLES = (DigestStyle.BRIEF, DigestStyle.BALANCED, DigestStyle.DEEP)
_DEPTHS = (TechnicalDepth.HIGH_LEVEL, TechnicalDepth.PRACTICAL, TechnicalDepth.DEEP)
_TOPIC_VALUES = frozenset(topic.value for topic in Topic)


class QuizError(ServiceError):
    INVALID = "invalid"
    ALREADY_ANSWERED = "already_answered"

    @override
    def service(self) -> str:
        return "quiz"


class QuizOption(Model):
    answer: str
    label: str


class Quiz(Model):
    question: QuizQuestion
    prompt: str
    options: tuple[QuizOption, ...]


class QuizOutcome(Model):
    profile: Profile
    interests: Interests
    message: str


_LENGTH_QUIZ = Quiz(
    question=QuizQuestion.LENGTH,
    prompt="How was the length of today's digest?",
    options=(
        QuizOption(answer=_LESS, label="Too long"),
        QuizOption(answer=_SAME, label="About right"),
        QuizOption(answer=_MORE, label="Too short"),
    ),
)
_DEPTH_QUIZ = Quiz(
    question=QuizQuestion.DEPTH,
    prompt="How technical should the summaries be?",
    options=(
        QuizOption(answer=_LESS, label="Less technical"),
        QuizOption(answer=_SAME, label="As they are"),
        QuizOption(answer=_MORE, label="More technical"),
    ),
)


def _topic_quiz(
    placements: Sequence[DigestEventPlacement], *, fewer: bool
) -> Quiz | None:
    counts = Counter(
        topic for placement in placements for topic in placement.event.topics
    )

    if len(counts) < _MIN_TOPIC_OPTIONS:
        return None

    return Quiz(
        question=QuizQuestion.FEWER_TOPICS if fewer else QuizQuestion.TOPICS,
        prompt="Which of today's topics do you want less of?"
        if fewer
        else "Which of today's topics do you want more of?",
        options=tuple(
            QuizOption(answer=topic.value, label=recommend.TOPIC_LABELS[topic])
            for topic, _ in counts.most_common(_TOPIC_OPTION_LIMIT)
        ),
    )


def build_quizzes(
    placements: Sequence[DigestEventPlacement], *, date: str
) -> list[Quiz]:
    """At most two questions for the foot of the digest: which of the day's topics to
    see more of, and in turn by day, which to see less of, the length or the depth, so
    topic weights can move both ways."""
    quizzes: list[Quiz] = []
    more = _topic_quiz(placements, fewer=False)

    if more is not None:
        quizzes.append(more)

    match date_type.fromisoformat(date).toordinal() % 3:
        case 0:
            quizzes.append(_topic_quiz(placements, fewer=True) or _LENGTH_QUIZ)
        case 1:
            quizzes.append(_LENGTH_QUIZ)
        case _:
            quizzes.append(_DEPTH_QUIZ)

    return quizzes


def _stepped[T](ladder: Sequence[T], current: T, step: int) -> T:
    position = ladder.index(current) + step

    return ladder[min(len(ladder) - 1, max(0, position))]


def resolve_answer(
    question: QuizQuestion, answer: str, profile: Profile, interests: Interests
) -> OnSuccess[QuizOutcome]:
    """What an answer changes; pure, so the caller decides when to persist."""
    match question:
        case QuizQuestion.TOPICS | QuizQuestion.FEWER_TOPICS:
            if answer not in _TOPIC_VALUES:
                return QuizError.INVALID

            topic = Topic(answer)
            fewer = question is QuizQuestion.FEWER_TOPICS
            step = settings.TOPIC_WEIGHT_QUIZ_STEP
            label = recommend.TOPIC_LABELS[topic].lower()

            return QuizOutcome(
                profile=profile,
                interests=interests.model_copy(
                    update={
                        "topic_weights": recommend.adjust_topic_weights(
                            interests.topic_weights, [topic], -step if fewer else step
                        )
                    }
                ),
                message=f"{'Less' if fewer else 'More'} on {label} from now on.",
            )
        case QuizQuestion.LENGTH:
            if answer not in _STEPS:
                return QuizError.INVALID

            style = _stepped(_STYLES, profile.digest_style, _STEPS[answer])

            return QuizOutcome(
                profile=profile.model_copy(update={"digest_style": style}),
                interests=interests,
                message=f"The digest edition is now {style.value}.",
            )
        case QuizQuestion.DEPTH:
            if answer not in _STEPS:
                return QuizError.INVALID

            depth = _stepped(_DEPTHS, interests.technical_depth, _STEPS[answer])

            return QuizOutcome(
                profile=profile,
                interests=interests.model_copy(update={"technical_depth": depth}),
                message=f"Technical depth is now {depth.value}.",
            )


async def apply_answer(
    ctx: AbstractContext,
    *,
    digest_id: str,
    question: QuizQuestion,
    answer: str,
    profile: Profile,
    interests: Interests,
    now: datetime,
) -> OnSuccess[QuizOutcome]:
    """Validates before recording, so a malformed link never uses up the question; the
    caller persists the returned profile and interests."""
    outcome = resolve_answer(question, answer, profile, interests)

    if is_error(outcome):
        return outcome

    recorded = await ctx.digests.record_quiz_answer(
        digest_id, question, answer, now=now
    )

    if is_error(recorded):
        return QuizError.ALREADY_ANSWERED

    logger.info(
        "Quiz answer recorded.",
        extra={"digest_id": digest_id, "question": question, "answer": answer},
    )

    return outcome
