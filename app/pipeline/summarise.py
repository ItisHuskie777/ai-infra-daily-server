import re
from collections.abc import Sequence
from typing import override

from pydantic import Field

from app import logging
from app.context import AbstractContext
from app.models import Comment
from app.models import DigestEventPlacement
from app.models import DigestStyle
from app.models import ItemKind
from app.models import Language
from app.models import Model
from app.models import StoredItem
from app.models import TechnicalDepth
from app.result import OnSuccess
from app.result import ServiceError
from app.result import is_error

logger = logging.get_logger(__name__)

_ITEM_TEXT_LIMIT = 1200
_MAX_COMMENTS_PER_ITEM = 8
_MAX_TITLE_LENGTH = 200
_WHITESPACE = re.compile(r"\s+")
_SYSTEM_PROMPT = (
    "You are a careful editor producing a neutral summary of one AI news event."
)

# Summary length targets in English words; Chinese targets are twice this in characters.
_LENGTH_TARGETS: dict[DigestStyle, int] = {
    DigestStyle.BRIEF: 60,
    DigestStyle.BALANCED: 120,
    DigestStyle.DEEP: 220,
}
_SUMMARY_CHARACTER_CAPS: dict[DigestStyle, int] = {
    DigestStyle.BRIEF: 800,
    DigestStyle.BALANCED: 1500,
    DigestStyle.DEEP: 2600,
}
_MAX_KEY_CLAIMS: dict[DigestStyle, int] = {
    DigestStyle.BRIEF: 3,
    DigestStyle.BALANCED: 5,
    DigestStyle.DEEP: 6,
}
_MAX_TAKES: dict[TechnicalDepth, int] = {
    TechnicalDepth.HIGH_LEVEL: 3,
    TechnicalDepth.PRACTICAL: 4,
    TechnicalDepth.DEEP: 5,
}
_ITEM_KIND_ORDER: dict[ItemKind, int] = {
    ItemKind.PRIMARY: 0,
    ItemKind.MEDIA: 1,
    ItemKind.COMMENTARY: 2,
    ItemKind.DISCUSSION: 3,
}
_DEPTH_GUIDANCE: dict[TechnicalDepth, str] = {
    TechnicalDepth.HIGH_LEVEL: (
        "Write for a busy reader: outcomes and implications, no implementation detail."
    ),
    TechnicalDepth.PRACTICAL: (
        "Write for a practitioner: name concrete capabilities, numbers and what to try."
    ),
    TechnicalDepth.DEEP: (
        "Write for a researcher or infrastructure engineer: architecture, benchmarks, "
        "trade-offs."
    ),
}


class SummariseError(ServiceError):
    LLM_FAILED = "llm_failed"
    EMPTY_EVENT = "empty_event"

    @override
    def service(self) -> str:
        return "summarise"


class Take(Model):
    quote: str
    author: str | None
    url: str
    stance: str


class EventSummary(Model):
    title: str
    summary: str
    key_claims: tuple[str, ...]
    takes: tuple[Take, ...]
    language: Language


class _QuoteSource(Model):
    text: str
    author: str | None
    url: str


class _ModelTake(Model):
    quote: str = Field(min_length=1)
    author: str = Field(min_length=1)
    stance: str = Field(min_length=1)


class _ModelSummary(Model):
    title: str = Field(min_length=1, max_length=_MAX_TITLE_LENGTH)
    summary: str = Field(min_length=1)
    key_claims: tuple[str, ...]
    takes: tuple[_ModelTake, ...]


def _normalise_whitespace(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _excerpt(text: str) -> str:
    flat = _normalise_whitespace(text)

    if len(flat) <= _ITEM_TEXT_LIMIT:
        return flat

    return f"{flat[:_ITEM_TEXT_LIMIT]}…"


def _select_items(placement: DigestEventPlacement) -> list[StoredItem]:
    wanted = set(placement.item_ids)
    selected = [item for item in placement.event.items if item.id in wanted]
    items = selected if selected else list(placement.event.items)

    return sorted(items, key=lambda item: _ITEM_KIND_ORDER[item.kind])


def _top_comments(item: StoredItem) -> list[Comment]:
    ranked = sorted(item.comments, key=lambda comment: comment.score or 0, reverse=True)

    return ranked[:_MAX_COMMENTS_PER_ITEM]


def _describe_item(item: StoredItem) -> str:
    lines = [
        f"- [{item.kind}] {item.title}",
        f"  source: {item.source_id} | author: {item.author or 'unknown'} | "
        f"language: {item.language}",
        f"  text: {_excerpt(item.text)}",
    ]

    for comment in _top_comments(item):
        author = comment.author or "anonymous"
        score = "" if comment.score is None else f" (score {comment.score})"
        text = _normalise_whitespace(comment.text)
        lines.append(f"  comment by {author}{score}: {text}")

    return "\n".join(lines)


def _length_instruction(style: DigestStyle, language: Language) -> str:
    target = _LENGTH_TARGETS[style]

    if language is Language.ZH:
        return f"Target length for the summary: about {target * 2} Chinese characters."

    return f"Target length for the summary: about {target} words."


def _build_prompt(
    placement: DigestEventPlacement,
    items: Sequence[StoredItem],
    language: Language,
    *,
    style: DigestStyle,
    depth: TechnicalDepth,
) -> str:
    coverage = [
        item for item in items if item.kind in (ItemKind.PRIMARY, ItemKind.MEDIA)
    ]
    voices = [
        item
        for item in items
        if item.kind in (ItemKind.COMMENTARY, ItemKind.DISCUSSION)
    ]
    output_language = "Chinese (Simplified)" if language is Language.ZH else "English"
    coverage_text = "\n".join(_describe_item(item) for item in coverage) or "(none)"
    voices_text = "\n".join(_describe_item(item) for item in voices) or "(none)"

    return "\n".join(
        [
            "Summarise one news event for a personal AI-infrastructure digest.",
            f"Event title: {placement.event.title}",
            "",
            "Rules:",
            f"- Write the title, summary and key claims in {output_language}.",
            "- Language policy: English items are summarised in English, Chinese "
            "items in Chinese, any other language in English; a mixed event follows "
            "the majority of its items.",
            "- The summary is neutral and factual; key claims are short statements "
            "made by the sources.",
            '- "takes" quotes individual commentary and comments that add a distinct '
            "viewpoint. Each quote MUST be copied verbatim from the input text "
            "below, in its original language, never translated or paraphrased. "
            "Omit takes rather than invent them. Label each take's stance with one "
            "or two English words (for example supportive, sceptical, neutral).",
            f"- {_DEPTH_GUIDANCE[depth]}",
            f"- {_length_instruction(style, language)}",
            f"- At most {_MAX_KEY_CLAIMS[style]} key claims and {_MAX_TAKES[depth]} "
            "takes.",
            "",
            "## Primary sources and media coverage",
            coverage_text,
            "",
            "## Individual commentary and community discussion",
            voices_text,
        ]
    )


def _quote_sources_for(items: Sequence[StoredItem]) -> list[_QuoteSource]:
    sources: list[_QuoteSource] = []

    for item in items:
        sources.append(_QuoteSource(text=item.title, author=item.author, url=item.url))
        sources.append(_QuoteSource(text=item.text, author=item.author, url=item.url))

        for comment in item.comments:
            sources.append(
                _QuoteSource(
                    text=comment.text,
                    author=comment.author,
                    url=comment.url or item.url,
                )
            )

    return sources


def _verify_takes(
    takes: Sequence[_ModelTake], sources: Sequence[_QuoteSource]
) -> list[Take]:
    """QA5-style guard: keeps only quotes found verbatim in the input, attributed from
    the input rather than from the model."""
    verified: list[Take] = []

    for take in takes:
        quote = _normalise_whitespace(take.quote)

        if quote == "":
            continue

        source = next(
            (
                candidate
                for candidate in sources
                if quote in _normalise_whitespace(candidate.text)
            ),
            None,
        )

        if source is None:
            continue

        verified.append(
            Take(
                quote=quote,
                author=source.author or take.author,
                url=source.url,
                stance=take.stance.strip(),
            )
        )

    return verified


def _cap_summary(text: str, style: DigestStyle) -> str:
    flat = text.strip()
    cap = _SUMMARY_CHARACTER_CAPS[style]

    if len(flat) <= cap:
        return flat

    return f"{flat[:cap].rstrip()}…"


def summary_language_for(items: Sequence[StoredItem]) -> Language:
    """FR6.4: Chinese items are summarised in Chinese, English and every other language
    in English. A mixed event follows the majority; the primary item breaks a tie."""
    chinese = sum(1 for item in items if item.language is Language.ZH)
    rest = len(items) - chinese

    if chinese > rest:
        return Language.ZH

    if chinese < rest:
        return Language.EN

    primary = next((item for item in items if item.kind is ItemKind.PRIMARY), None)

    if primary is not None and primary.language is Language.ZH:
        return Language.ZH

    return Language.EN


async def summarise_event(
    ctx: AbstractContext,
    placement: DigestEventPlacement,
    *,
    style: DigestStyle,
    depth: TechnicalDepth,
) -> OnSuccess[EventSummary]:
    items = _select_items(placement)

    if not items:
        return SummariseError.EMPTY_EVENT

    language = summary_language_for(items)
    prompt = _build_prompt(placement, items, language, style=style, depth=depth)
    generated = await ctx.llm.complete_json(
        system=_SYSTEM_PROMPT, user=prompt, schema=_ModelSummary, temperature=0.3
    )

    if is_error(generated):
        logger.error(
            "Event summary failed.",
            extra={"event_id": placement.event.id, "error": str(generated)},
        )

        return SummariseError.LLM_FAILED

    takes = _verify_takes(generated.takes, _quote_sources_for(items))
    dropped = len(generated.takes) - len(takes)

    if dropped > 0:
        logger.warning(
            "Dropped takes not found in input.",
            extra={"event_id": placement.event.id, "dropped": dropped},
        )

    claims = [claim.strip() for claim in generated.key_claims if claim.strip() != ""]

    return EventSummary(
        title=generated.title.strip(),
        summary=_cap_summary(generated.summary, style),
        key_claims=tuple(claims[: _MAX_KEY_CLAIMS[style]]),
        takes=tuple(takes[: _MAX_TAKES[depth]]),
        language=language,
    )
