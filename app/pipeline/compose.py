from collections.abc import Callable
from collections.abc import Sequence
from datetime import date as date_type
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment
from jinja2 import FileSystemLoader

from app import settings
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import FeedbackSignal
from app.models import ItemKind
from app.models import Model
from app.models import SourceHealth
from app.models import StoredItem
from app.models import Thread
from app.pipeline import _items
from app.pipeline import feedback_token
from app.pipeline.quiz import Quiz
from app.pipeline.summarise import SummarisedEvent

type LinkSigner = Callable[[str], str]
"""Returns the token for one link, given the scope that link is allowed to act on."""

_DIGEST_NAME = "AI Infra Daily"
_TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
_SECTION_TITLES: dict[DigestSection, str] = {
    DigestSection.MODELS: "Models and releases",
    DigestSection.RESEARCH: "Research",
    DigestSection.INFRASTRUCTURE: "Infrastructure",
    DigestSection.AGENTS: "Agents and tools",
    DigestSection.BUSINESS: "Business and products",
    DigestSection.POLICY: "Policy and safety",
    DigestSection.OTHER: "Other",
}
_FEEDBACK_LABELS: dict[FeedbackSignal, str] = {
    FeedbackSignal.MORE_LIKE_THIS: "more like this",
    FeedbackSignal.LESS_LIKE_THIS: "less like this",
    FeedbackSignal.MUTE_SOURCE: "mute source",
    FeedbackSignal.PIN_THREAD: "pin thread",
}
_ENVIRONMENT = Environment(loader=FileSystemLoader(_TEMPLATES), autoescape=True)


class ComposedDigest(Model):
    subject: str
    html: str


class _FeedbackLink(Model):
    label: str
    url: str


class _TakeView(Model):
    quote: str
    author: str
    url: str
    stance: str


class _EventView(Model):
    title: str
    url: str | None
    language: str
    day_badge: str | None
    summary: str
    key_claims: tuple[str, ...]
    takes: tuple[_TakeView, ...]
    feedback: tuple[_FeedbackLink, ...]


class _QuizOptionView(Model):
    label: str
    url: str


class _QuizView(Model):
    prompt: str
    options: tuple[_QuizOptionView, ...]


class _SectionView(Model):
    title: str
    events: tuple[_EventView, ...]


def _by_section(
    events: Sequence[SummarisedEvent],
) -> list[tuple[DigestSection, list[SummarisedEvent]]]:
    """The sections that have events, in the order the email shows them."""
    grouped = [
        (section, [event for event in events if event.placement.section is section])
        for section in DigestSection
    ]

    return [(section, members) for section, members in grouped if members]


def _primary_item(placement: DigestEventPlacement) -> StoredItem | None:
    pool = _items.chosen_items(placement)

    for kind in (ItemKind.PRIMARY, ItemKind.MEDIA):
        for item in pool:
            if item.kind is kind:
                return item

    return pool[0] if pool else None


def _feedback_url(
    base_url: str,
    sign: LinkSigner,
    signal: FeedbackSignal,
    event: SummarisedEvent,
    primary: StoredItem | None,
) -> str:
    event_id = event.placement.event.id
    source_id = None if primary is None else primary.source_id
    item_id = None if primary is None else primary.id
    parameters = {
        "token": sign(
            feedback_token.feedback_scope(signal.value, event_id, source_id, item_id)
        ),
        "signal": signal.value,
        "event": event_id,
    }

    if source_id is not None and item_id is not None:
        parameters["source"] = source_id
        parameters["item"] = item_id

    return f"{base_url}/feedback?{urlencode(parameters)}"


def _feedback_links(
    base_url: str, sign: LinkSigner, event: SummarisedEvent, primary: StoredItem | None
) -> tuple[_FeedbackLink, ...]:
    # NOTE: Two links per event double the size of a compact digest of sixty events,
    # past the point where mail clients clip the message; the quiz collects taste.
    if settings.DIGEST_COMPACT:
        return ()

    signals = [
        FeedbackSignal.MORE_LIKE_THIS,
        FeedbackSignal.LESS_LIKE_THIS,
        FeedbackSignal.MUTE_SOURCE,
    ]

    if event.placement.event.thread is not None:
        signals.append(FeedbackSignal.PIN_THREAD)

    return tuple(
        _FeedbackLink(
            label=_FEEDBACK_LABELS[signal],
            url=_feedback_url(base_url, sign, signal, event, primary),
        )
        for signal in signals
    )


def _day_badge(thread: Thread | None) -> str | None:
    if thread is None or thread.day_count <= 1:
        return None

    return f"Day {thread.day_count} of this story"


def _event_view(
    event: SummarisedEvent, *, base_url: str, sign: LinkSigner
) -> _EventView:
    primary = _primary_item(event.placement)

    return _EventView(
        title=event.summary.title,
        url=None if primary is None else primary.url,
        language=event.summary.language.value,
        day_badge=_day_badge(event.placement.event.thread),
        summary=event.summary.summary,
        key_claims=event.summary.key_claims,
        takes=tuple(
            _TakeView(
                quote=take.quote,
                author=take.author or "unattributed",
                url=take.url,
                stance=take.stance,
            )
            for take in event.summary.takes
        ),
        feedback=_feedback_links(base_url, sign, event, primary),
    )


def _date_label(date: str) -> str:
    day = date_type.fromisoformat(date)

    return f"{day.day} {day:%B %Y}"


def _subject_for(date: str, title: str | None) -> str:
    """The lead story's headline goes in the subject, so the inbox shows the news."""
    if title is None:
        return f"{_DIGEST_NAME}, {_date_label(date)}"

    return f"{_DIGEST_NAME}, {_date_label(date)}: {title}"


def _quiz_view(quiz: Quiz, *, base_url: str, sign: LinkSigner) -> _QuizView:
    return _QuizView(
        prompt=quiz.prompt,
        options=tuple(
            _QuizOptionView(
                label=option.label,
                url=f"{base_url}/quiz?"
                + urlencode(
                    {
                        "token": sign(
                            feedback_token.quiz_scope(
                                quiz.question.value, option.answer
                            )
                        ),
                        "question": quiz.question.value,
                        "answer": option.answer,
                    }
                ),
            )
            for option in quiz.options
        ),
    )


def compose_digest_html(
    *,
    date: str,
    events: Sequence[SummarisedEvent],
    health: Sequence[SourceHealth],
    feedback_base_url: str,
    sign_link: LinkSigner,
    quizzes: Sequence[Quiz] = (),
) -> ComposedDigest:
    top_title = (
        max(
            events, key=lambda event: event.placement.event.relevance_score
        ).summary.title
        if events
        else None
    )
    subject = _subject_for(date, top_title)
    base_url = feedback_base_url.rstrip("/")
    views = [
        _SectionView(
            title=_SECTION_TITLES[section],
            events=tuple(
                _event_view(event, base_url=base_url, sign=sign_link)
                for event in members
            ),
        )
        for section, members in _by_section(events)
    ]
    failed = [source for source in health if not source.ok]
    html = _ENVIRONMENT.get_template("digest.html.j2").render(
        digest_name=_DIGEST_NAME,
        subject=subject,
        date_label=_date_label(date),
        story_count=sum(len(view.events) for view in views),
        sections=views,
        quizzes=[
            _quiz_view(quiz, base_url=base_url, sign=sign_link) for quiz in quizzes
        ],
        health_total=len(health),
        health_ok=len(health) - len(failed),
        failed_sources=failed,
    )

    return ComposedDigest(subject=subject, html=html)
