from app.sources._common import ImplementsSourceAdapter
from app.sources.bluesky import BlueskyAdapter
from app.sources.hn import HackerNewsAdapter
from app.sources.reddit import RedditAdapter
from app.sources.rss import RssAdapter
from app.sources.x import XAdapter
from app.sources.xiaohongshu import XiaohongshuAdapter
from app.sources.youtube import YoutubeAdapter

ADAPTERS: dict[str, ImplementsSourceAdapter] = {
    adapter.kind: adapter
    for adapter in (
        RssAdapter(),
        HackerNewsAdapter(),
        RedditAdapter(),
        BlueskyAdapter(),
        YoutubeAdapter(),
        XiaohongshuAdapter(),
        XAdapter(),
    )
}
