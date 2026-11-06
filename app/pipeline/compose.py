from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment
from jinja2 import FileSystemLoader

from app import settings
from app.models import DigestEventPlacement
from app.models import DigestSection
from app.models import DigestStyle
from app.models import FeedbackSignal
from app.models import ItemKind
from app.models import Model
from app.models import SourceHealth
from app.models import StoredItem
from app.models import Thread
from app.pipeline.quiz import Quiz
from app.pipeline.summarise import EventSummary

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


class ComposedEvent(Model):
    placement: DigestEventPlacement
    summary: EventSummary
    thread: Thread | None


class ComposedSection(Model):
    section: DigestSection
    events: tuple[ComposedEvent, ...]


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


def _ordered_sections(sections: Sequence[ComposedSection]) -> list[ComposedSection]:
    ordered: list[ComposedSection] = []

    for section in DigestSection:
        events = tuple(
            event
            for candidate in sections
            if candidate.section is section
            for event in candidate.events
        )

        if events:
            ordered.append(ComposedSection(section=section, events=events))

    return ordered


def _primary_item(placement: DigestEventPlacement) -> StoredItem | None:
    wanted = set(placement.item_ids)
    selected = [item for item in placement.event.items if item.id in wanted]
    pool = selected if selected else list(placement.event.items)

    for kind in (ItemKind.PRIMARY, ItemKind.MEDIA):
        for item in pool:
            if item.kind is kind:
                return item

    return pool[0] if pool else None


def _feedback_url(
    base_url: str,
    token: str,
    signal: FeedbackSignal,
    event: ComposedEvent,
    primary: StoredItem | None,
) -> str:
    parameters = {
        "token": token,
        "signal": signal.value,
        "event": event.placement.event.id,
    }

    if primary is not None:
        parameters["source"] = primary.source_id
        parameters["item"] = primary.id

    return f"{base_url}/feedback?{urlencode(parameters)}"


def _feedback_links(
    base_url: str, token: str, event: ComposedEvent, primary: StoredItem | None
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

    if event.thread is not None:
        signals.append(FeedbackSignal.PIN_THREAD)

    return tuple(
        _FeedbackLink(
            label=_FEEDBACK_LABELS[signal],
            url=_feedback_url(base_url, token, signal, event, primary),
        )
        for signal in signals
    )


def _day_badge(thread: Thread | None) -> str | None:
    if thread is None or thread.day_count <= 1:
        return None

    return f"Day {thread.day_count} of this story"


def _event_view(event: ComposedEvent, *, base_url: str, token: str) -> _EventView:
    primary = _primary_item(event.placement)

    return _EventView(
        title=event.summary.title,
        url=None if primary is None else primary.url,
        language=event.summary.language.value,
        day_badge=_day_badge(event.thread),
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
        feedback=_feedback_links(base_url, token, event, primary),
    )


def _subject_for(date: str, title: str | None) -> str:
    if title is None:
        return f"{_DIGEST_NAME} — {date}"

    return f"{_DIGEST_NAME} — {date} — {title}"


def _quiz_view(quiz: Quiz, *, base_url: str, token: str) -> _QuizView:
    return _QuizView(
        prompt=quiz.prompt,
        options=tuple(
            _QuizOptionView(
                label=option.label,
                url=f"{base_url}/quiz?"
                + urlencode(
                    {
                        "token": token,
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
    style: DigestStyle,
    sections: Sequence[ComposedSection],
    health: Sequence[SourceHealth],
    feedback_base_url: str,
    feedback_token: str,
    quizzes: Sequence[Quiz] = (),
) -> ComposedDigest:
    ordered = _ordered_sections(sections)
    events = [event for section in ordered for event in section.events]
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
            title=_SECTION_TITLES[section.section],
            events=tuple(
                _event_view(event, base_url=base_url, token=feedback_token)
                for event in section.events
            ),
        )
        for section in ordered
    ]
    failed = [source for source in health if not source.ok]
    html = _ENVIRONMENT.get_template("digest.html.j2").render(
        digest_name=_DIGEST_NAME,
        subject=subject,
        date=date,
        style=style.value,
        sections=views,
        quizzes=[
            _quiz_view(quiz, base_url=base_url, token=feedback_token)
            for quiz in quizzes
        ],
        health_total=len(health),
        health_ok=len(health) - len(failed),
        failed_sources=failed,
    )

    return ComposedDigest(subject=subject, html=html)
