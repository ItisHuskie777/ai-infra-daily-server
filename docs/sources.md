# Seeded sources

`app/sources/seeds.py` seeds 48 sources on first run (`aid seed`); the owner edits them with
`aid sources` or `PATCH /api/sources/{id}` afterwards, and `aid seed` never overwrites edits.

## Live audit, 2026-11-05

36 of the 46 enabled seeds returned items (774 unique items in one fetch). The following seeds
failed for data reasons and need a replacement URL or handle from the owner:

| Source id               | Problem                                            |
| ----------------------- | -------------------------------------------------- |
| `bsky-sebastianraschka` | Bluesky "Profile not found" (wrong handle)         |
| `bsky-ylecun`           | Bluesky "Profile not found" (wrong handle)         |
| `meta-ai-blog`          | HTTP 404                                           |
| `mistral-news`          | HTTP 404                                           |
| `the-batch`             | HTTP 404                                           |
| `yt-karpathy`           | HTTP 404 (channel id)                              |
| `jiqizhixin`            | Returns an HTML page, not a feed                   |
| `reddit-machinelearning`| HTTP 403 for the project user agent                |
| `reddit-localllama`     | HTTP 403 for the project user agent                |
| `bsky-jeremyphoward`    | Works but returned no original posts (reposts only)|

The Xiaohongshu and X seeds are disabled until a cookie is configured; see
[xiaohongshu.md](xiaohongshu.md) and [x.md](x.md).
