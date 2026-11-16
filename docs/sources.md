# Seeded sources

`app/sources/seeds.py` seeds 49 sources on first run (`aid seed`); the owner edits them with
`aid sources` or `PATCH /api/sources/{id}` afterwards. Seeding runs at the start of every
pipeline run: it inserts missing sources and updates the name, category, language and
config of existing ones from the seed file, so a corrected feed address reaches a database
seeded earlier. It never touches `enabled` or `weight`, the two fields the owner edits.

## Live audit, 2026-11-05

All 47 enabled seeds returned items, both from a residential connection and from the
production server (946 unique items in one fetch). The two remaining seeds, Xiaohongshu
and X, stay disabled until a cookie is configured; see [xiaohongshu.md](xiaohongshu.md)
and [x.md](x.md).

An earlier audit the same day found ten broken seeds. What replaced them:

| Source id                | Was                                   | Now                                                  |
| ------------------------ | ------------------------------------- | ---------------------------------------------------- |
| `bsky-sebastianraschka`  | Profile not found                     | Handle `rasbt.bsky.social`                           |
| `bsky-ylecun`            | Profile not found                     | Handle `yann-lecun.bsky.social`                      |
| `bsky-jeremyphoward`     | Stale handle, reposts only            | Handle `howard.fm`                                   |
| `yt-karpathy`            | HTTP 404 (mistyped channel id)        | Channel `UCXUPKJO5MZQN11PqgIvyuvQ`                   |
| `mistral-news`           | HTTP 404                              | Official feed `https://mistral.ai/rss.xml`           |
| `the-batch`              | HTTP 404                              | Official feed `https://charonhub.deeplearning.ai/rss/` |
| `meta-ai-blog`           | HTTP 404, Meta AI publishes no feed   | Replaced by `meta-ai-research` (Engineering at Meta) |
| `jiqizhixin`             | Publishes no feed                     | Replaced by `infoq-cn` and `zhidx`                   |
| `reddit-machinelearning` | HTTP 403 on the anonymous JSON API    | Public Atom feed (see below)                         |
| `reddit-localllama`      | HTTP 403 on the anonymous JSON API    | Public Atom feed (see below)                         |

## Reddit

Reddit answers anonymous requests to its JSON endpoints with 403 and allows about one
request a minute to the Atom feeds. The adapter therefore reads
`/r/<subreddit>/top/.rss?t=day`, one subreddit at a time, and waits out the window
Reddit announces in `x-ratelimit-reset` before the next request. A subreddit whose turn
would come after the source deadline (`SOURCE_DEADLINE_SECONDS`) is reported as
`rate_limited` at once instead of waiting to be cut off. The feed has no scores
and no comments, so Reddit items carry neither; the day's `top` ordering and the
`limit` in the source config stand in for a score threshold. Scores and top comments
would need Reddit's OAuth API with an app id created by the owner.
