from app.context import AbstractContext
from app.models import BlueskyConfig
from app.models import HackerNewsConfig
from app.models import Language
from app.models import RedditConfig
from app.models import RssConfig
from app.models import Source
from app.models import SourceCategory
from app.models import SourceConfig
from app.models import XConfig
from app.models import XiaohongshuConfig
from app.models import YoutubeConfig

# Individual voices rank above community, vendors above media, papers lowest (FR5.2).
_CATEGORY_WEIGHTS: dict[SourceCategory, float] = {
    SourceCategory.AUTHOR: 1.3,
    SourceCategory.COMMUNITY: 1.0,
    SourceCategory.VENDOR: 1.1,
    SourceCategory.MEDIA: 0.9,
    SourceCategory.PAPER: 0.8,
}
_HN_COMMENT_LIMIT = 5
_REDDIT_POST_LIMIT = 15


def _seed(
    id: str,
    name: str,
    category: SourceCategory,
    config: SourceConfig,
    *,
    language: Language = Language.EN,
    enabled: bool = True,
) -> Source:
    return Source(
        id=id,
        name=name,
        category=category,
        language=language,
        enabled=enabled,
        weight=_CATEGORY_WEIGHTS[category],
        config=config,
    )


def _rss(
    id: str,
    name: str,
    category: SourceCategory,
    url: str,
    *,
    language: Language = Language.EN,
) -> Source:
    return _seed(id, name, category, RssConfig(url=url), language=language)


def _hn_search(id: str, query: str, min_points: int) -> Source:
    return _seed(
        id,
        f"Hacker News: {query}",
        SourceCategory.COMMUNITY,
        HackerNewsConfig(
            mode="search",
            query=query,
            min_points=min_points,
            comment_limit=_HN_COMMENT_LIMIT,
        ),
    )


def _reddit(subreddit: str) -> Source:
    return _seed(
        f"reddit-{subreddit.lower()}",
        f"r/{subreddit}",
        SourceCategory.COMMUNITY,
        RedditConfig(
            subreddit=subreddit,
            sort="top",
            limit=_REDDIT_POST_LIMIT,
        ),
    )


def _bluesky(id: str, name: str, handle: str) -> Source:
    return _seed(
        id, name, SourceCategory.AUTHOR, BlueskyConfig(mode="author", handle=handle)
    )


def _youtube(id: str, name: str, channel_id: str) -> Source:
    return _seed(id, name, SourceCategory.AUTHOR, YoutubeConfig(channel_id=channel_id))


_VENDOR_SOURCES: tuple[Source, ...] = (
    _rss(
        "openai-news",
        "OpenAI News",
        SourceCategory.VENDOR,
        "https://openai.com/news/rss.xml",
    ),
    # NOTE: Anthropic publishes no feed of its own; this is a community-maintained
    # mirror of its news page.
    _rss(
        "anthropic-news",
        "Anthropic News",
        SourceCategory.VENDOR,
        "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml",
    ),
    _rss(
        "deepmind-blog",
        "Google DeepMind Blog",
        SourceCategory.VENDOR,
        "https://deepmind.google/blog/rss.xml",
    ),
    _rss(
        "google-ai-blog",
        "Google AI Blog",
        SourceCategory.VENDOR,
        "https://blog.google/technology/ai/rss/",
    ),
    _rss(
        "meta-ai-research",
        "Engineering at Meta: AI Research",
        SourceCategory.VENDOR,
        "https://engineering.fb.com/category/ai-research/feed/",
    ),
    _rss(
        "nvidia-technical-blog",
        "NVIDIA Technical Blog",
        SourceCategory.VENDOR,
        "https://developer.nvidia.com/blog/feed/",
    ),
    _rss(
        "huggingface-blog",
        "Hugging Face Blog",
        SourceCategory.VENDOR,
        "https://huggingface.co/blog/feed.xml",
    ),
    _rss(
        "cloudflare-blog",
        "Cloudflare Blog",
        SourceCategory.VENDOR,
        "https://blog.cloudflare.com/rss/",
    ),
    _rss(
        "microsoft-research",
        "Microsoft Research",
        SourceCategory.VENDOR,
        "https://www.microsoft.com/en-us/research/feed/",
    ),
    _rss(
        "mistral-news",
        "Mistral AI News",
        SourceCategory.VENDOR,
        "https://mistral.ai/rss.xml",
    ),
)

_MEDIA_SOURCES: tuple[Source, ...] = (
    _rss(
        "verge-ai",
        "The Verge AI",
        SourceCategory.MEDIA,
        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    ),
    _rss(
        "mit-tech-review-ai",
        "MIT Technology Review AI",
        SourceCategory.MEDIA,
        "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    ),
    _rss(
        "ars-technica-ai",
        "Ars Technica AI",
        SourceCategory.MEDIA,
        "https://arstechnica.com/ai/feed/",
    ),
    _rss(
        "techcrunch-ai",
        "TechCrunch AI",
        SourceCategory.MEDIA,
        "https://techcrunch.com/category/artificial-intelligence/feed/",
    ),
    _rss(
        "infoq-cn",
        "InfoQ 中文",
        SourceCategory.MEDIA,
        "https://www.infoq.cn/feed",
        language=Language.ZH,
    ),
    _rss(
        "zhidx",
        "智东西",
        SourceCategory.MEDIA,
        "https://zhidx.com/rss",
        language=Language.ZH,
    ),
    _rss(
        "qbitai",
        "量子位",
        SourceCategory.MEDIA,
        "https://www.qbitai.com/feed",
        language=Language.ZH,
    ),
)

_PAPER_SOURCES: tuple[Source, ...] = (
    _rss(
        "arxiv-cs-ai",
        "arXiv cs.AI",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/cs.AI",
    ),
    _rss(
        "arxiv-cs-lg",
        "arXiv cs.LG",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/cs.LG",
    ),
    _rss(
        "arxiv-cs-cl",
        "arXiv cs.CL",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/cs.CL",
    ),
    _rss(
        "arxiv-cs-dc",
        "arXiv cs.DC",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/cs.DC",
    ),
    # Networking and signal processing, where O-RAN and 6G papers are filed.
    _rss(
        "arxiv-cs-ni",
        "arXiv cs.NI",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/cs.NI",
    ),
    _rss(
        "arxiv-eess-sp",
        "arXiv eess.SP",
        SourceCategory.PAPER,
        "http://export.arxiv.org/rss/eess.SP",
    ),
)

_COMMUNITY_SOURCES: tuple[Source, ...] = (
    _seed(
        "hn-front",
        "Hacker News front page",
        SourceCategory.COMMUNITY,
        HackerNewsConfig(mode="front", min_points=100, comment_limit=_HN_COMMENT_LIMIT),
    ),
    _hn_search("hn-search-llm", "LLM", 50),
    _hn_search("hn-search-gpu", "GPU", 50),
    _hn_search("hn-search-inference", "inference", 50),
    _reddit("MachineLearning"),
    _reddit("LocalLLaMA"),
)

_AUTHOR_SOURCES: tuple[Source, ...] = (
    _rss(
        "simon-willison",
        "Simon Willison",
        SourceCategory.AUTHOR,
        "https://simonwillison.net/atom/everything/",
    ),
    _rss(
        "import-ai",
        "Import AI",
        SourceCategory.AUTHOR,
        "https://importai.substack.com/feed",
    ),
    _rss(
        "the-batch",
        "The Batch",
        SourceCategory.AUTHOR,
        "https://charonhub.deeplearning.ai/rss/",
    ),
    _rss(
        "latent-space",
        "Latent Space",
        SourceCategory.AUTHOR,
        "https://www.latent.space/feed",
    ),
    _rss(
        "interconnects",
        "Interconnects",
        SourceCategory.AUTHOR,
        "https://www.interconnects.ai/feed",
    ),
    _rss(
        "ahead-of-ai",
        "Ahead of AI",
        SourceCategory.AUTHOR,
        "https://magazine.sebastianraschka.com/feed",
    ),
    _rss(
        "semianalysis",
        "SemiAnalysis",
        SourceCategory.AUTHOR,
        "https://semianalysis.com/feed/",
    ),
    _rss(
        "one-useful-thing",
        "One Useful Thing",
        SourceCategory.AUTHOR,
        "https://www.oneusefulthing.org/feed",
    ),
    _bluesky("bsky-simonwillison", "Simon Willison (Bluesky)", "simonwillison.net"),
    _bluesky("bsky-karpathy", "Andrej Karpathy (Bluesky)", "karpathy.bsky.social"),
    _bluesky("bsky-swyx", "swyx (Bluesky)", "swyx.io"),
    _bluesky("bsky-natolambert", "Nathan Lambert (Bluesky)", "natolambert.bsky.social"),
    _bluesky("bsky-ylecun", "Yann LeCun (Bluesky)", "yann-lecun.bsky.social"),
    _bluesky("bsky-emollick", "Ethan Mollick (Bluesky)", "emollick.bsky.social"),
    _bluesky("bsky-jeremyphoward", "Jeremy Howard (Bluesky)", "howard.fm"),
    _bluesky("bsky-hardmaru", "David Ha (Bluesky)", "hardmaru.bsky.social"),
    _bluesky(
        "bsky-sebastianraschka", "Sebastian Raschka (Bluesky)", "rasbt.bsky.social"
    ),
    # NOTE: Channel ids come from the channels' public pages; a wrong id surfaces as a
    # failed source.
    _youtube("yt-karpathy", "Andrej Karpathy (YouTube)", "UCXUPKJO5MZQN11PqgIvyuvQ"),
    _youtube(
        "yt-yannic-kilcher", "Yannic Kilcher (YouTube)", "UCZHmQk67mSJgfCCTn7xBfew"
    ),
    _youtube("yt-ai-explained", "AI Explained (YouTube)", "UCNJ1Ymd5yFuUPtn21xtRbbw"),
)

# Session-bound platforms stay off until the owner supplies a cookie and enables them.
_SESSION_SOURCES: tuple[Source, ...] = (
    _seed(
        "xiaohongshu-following",
        "Xiaohongshu following",
        SourceCategory.AUTHOR,
        XiaohongshuConfig(mode="following", comment_limit=10),
        language=Language.ZH,
        enabled=False,
    ),
    _seed(
        "x-home",
        "X home timeline",
        SourceCategory.AUTHOR,
        XConfig(mode="home"),
        enabled=False,
    ),
)

DEFAULT_SOURCES: tuple[Source, ...] = (
    *_VENDOR_SOURCES,
    *_MEDIA_SOURCES,
    *_PAPER_SOURCES,
    *_COMMUNITY_SOURCES,
    *_AUTHOR_SOURCES,
    *_SESSION_SOURCES,
)


async def seed_default_sources(ctx: AbstractContext) -> int:
    """Inserts the seeds the database lacks; the owner's edits to existing rows are
    never overwritten."""
    return await ctx.sources.insert_missing(DEFAULT_SOURCES, now=ctx.clock.now())
