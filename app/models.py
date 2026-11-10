from datetime import datetime
from enum import StrEnum
from typing import Annotated
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


class Language(StrEnum):
    EN = "en"
    ZH = "zh"
    OTHER = "other"


class SourceCategory(StrEnum):
    MEDIA = "media"
    VENDOR = "vendor"
    AUTHOR = "author"
    COMMUNITY = "community"
    PAPER = "paper"


class ItemKind(StrEnum):
    """How an item relates to a story; drives ordering inside an event (FR3.2)."""

    PRIMARY = "primary"
    MEDIA = "media"
    COMMENTARY = "commentary"
    DISCUSSION = "discussion"


class DigestSection(StrEnum):
    """Categories of the digest, in the order the email shows them."""

    MODELS = "models"
    RESEARCH = "research"
    INFRASTRUCTURE = "infrastructure"
    AGENTS = "agents"
    BUSINESS = "business"
    POLICY = "policy"
    OTHER = "other"


class DigestStyle(StrEnum):
    BRIEF = "brief"
    BALANCED = "balanced"
    DEEP = "deep"


class TechnicalDepth(StrEnum):
    HIGH_LEVEL = "high-level"
    PRACTICAL = "practical"
    DEEP = "deep"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class FeedbackSignal(StrEnum):
    MORE_LIKE_THIS = "more_like_this"
    LESS_LIKE_THIS = "less_like_this"
    MUTE_SOURCE = "mute_source"
    PIN_THREAD = "pin_thread"


class Topic(StrEnum):
    """Fixed taxonomy every event is tagged with; the owner's taste is a weight per
    topic, learned from feedback and quiz answers."""

    MODELS = "models"
    INFERENCE = "inference"
    HARDWARE = "hardware"
    TRAINING = "training"
    AGENTS = "agents"
    TOOLING = "tooling"
    RESEARCH = "research"
    OPEN_SOURCE = "open_source"
    POLICY = "policy"
    BUSINESS = "business"
    PRODUCTS = "products"


class QuizQuestion(StrEnum):
    TOPICS = "topics"
    FEWER_TOPICS = "fewer_topics"
    LENGTH = "length"
    DEPTH = "depth"


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RssConfig(Model):
    kind: Literal["rss"] = "rss"
    url: str


class HackerNewsConfig(Model):
    kind: Literal["hn"] = "hn"
    mode: Literal["front", "search"]
    query: str | None = None
    min_points: int = Field(ge=0)
    comment_limit: int = Field(ge=0)


class RedditConfig(Model):
    kind: Literal["reddit"] = "reddit"
    subreddit: str
    sort: Literal["hot", "top", "new"]
    limit: int = Field(ge=1, le=100)


class BlueskyConfig(Model):
    kind: Literal["bluesky"] = "bluesky"
    mode: Literal["author", "feed"]
    handle: str | None = None
    feed_uri: str | None = None


class YoutubeConfig(Model):
    kind: Literal["youtube"] = "youtube"
    channel_id: str


class XiaohongshuConfig(Model):
    kind: Literal["xiaohongshu"] = "xiaohongshu"
    mode: Literal["following", "user"]
    user_id: str | None = None
    comment_limit: int = Field(ge=0)


class XConfig(Model):
    kind: Literal["x"] = "x"
    mode: Literal["home", "user"]
    handle: str | None = None


type SourceConfig = Annotated[
    RssConfig
    | HackerNewsConfig
    | RedditConfig
    | BlueskyConfig
    | YoutubeConfig
    | XiaohongshuConfig
    | XConfig,
    Field(discriminator="kind"),
]

type SourceKind = Literal[
    "rss", "hn", "reddit", "bluesky", "youtube", "xiaohongshu", "x"
]


class Source(Model):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: SourceCategory
    language: Language
    enabled: bool
    weight: float = Field(gt=0, le=2)
    config: SourceConfig

    @property
    def kind(self) -> SourceKind:
        return self.config.kind


class Engagement(Model):
    likes: int | None = None
    comments: int | None = None
    score: int | None = None
    shares: int | None = None


class Comment(Model):
    id: str = Field(min_length=1)
    author: str | None = None
    text: str = Field(min_length=1)
    score: int | None = None
    url: str | None = None


class SourceItem(Model):
    """A normalised piece of content; `id` is the sha-256 hex of `canonical_url`."""

    id: str = Field(min_length=64, max_length=64)
    source_id: str = Field(min_length=1)
    kind: ItemKind
    url: str
    canonical_url: str
    title: str = Field(min_length=1)
    author: str | None = None
    language: Language
    published_at: datetime
    text: str
    engagement: Engagement | None = None
    comments: tuple[Comment, ...] = ()
    tags: tuple[str, ...] = ()


class StoredItem(SourceItem):
    first_seen_at: datetime
    embedded: bool
    event_id: str | None


class Event(Model):
    id: str
    thread_id: str | None
    title: str
    summary: str
    created_at: datetime
    updated_at: datetime
    centroid_item_id: str | None


class Thread(Model):
    id: str
    title: str
    first_seen_at: datetime
    last_seen_at: datetime
    day_count: int
    pinned: bool
    pinned_query: str | None


class EventWithItems(Event):
    items: tuple[StoredItem, ...]


class EventScore(Model):
    """The model's judgement of one event, kept until the event or the interest profile
    changes (`signature`)."""

    relevance: float
    rationale: str
    topics: tuple[Topic, ...]
    signature: str


class RankedEvent(EventWithItems):
    relevance_score: float
    rationale: str
    thread: Thread | None
    topics: tuple[Topic, ...] = ()


class DigestEventPlacement(Model):
    event: RankedEvent
    section: DigestSection
    position: int
    item_ids: tuple[str, ...]


class SourceHealth(Model):
    source_id: str
    name: str
    ok: bool
    error: str | None
    item_count: int


class Digest(Model):
    id: str
    date: str
    style: DigestStyle
    subject: str
    html: str
    placements: tuple[DigestEventPlacement, ...]
    health: tuple[SourceHealth, ...]
    delivery_status: DeliveryStatus
    delivery_error: str | None
    created_at: datetime


class Feedback(Model):
    created_at: datetime
    digest_id: str | None
    event_id: str | None
    item_id: str | None
    source_id: str | None
    signal: FeedbackSignal


class Profile(Model):
    email: str
    timezone: str
    delivery_hour: int = Field(ge=0, le=23)
    digest_style: DigestStyle
    last_digest_date: str | None


class Interests(Model):
    topics: tuple[str, ...] = ()
    keywords_include: tuple[str, ...] = ()
    keywords_exclude: tuple[str, ...] = ()
    followed_authors: tuple[str, ...] = ()
    preferred_sources: tuple[str, ...] = ()
    blocked_sources: tuple[str, ...] = ()
    technical_depth: TechnicalDepth = TechnicalDepth.PRACTICAL
    # A missing topic counts as 1.0; above favours it, below holds it back.
    topic_weights: dict[Topic, float] = Field(default_factory=dict)
