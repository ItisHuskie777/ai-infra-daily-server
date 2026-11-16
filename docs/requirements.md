# AI Infra Daily — Requirements

Status: v3, 2026-11-10. Requirements for the Python service in this repository; it replaces an earlier Cloudflare Workers prototype.

## Purpose

A personal, single-user system that emails one digest per day covering AI news and
discussion. Mainstream media is in scope, but the distinctive value is the
**individual voice**: posts from bloggers, independent accounts and
high-engagement comments that carry first-hand experience and interpretation of
the big narratives. The reference experience is reading Xiaohongshu (RedNote)
daily: following a set of authors, reading their new posts, and reading the
top-voted comments.

## Stakeholders

| Stakeholder                  | Concern                                                                                                   |
| ---------------------------- | --------------------------------------------------------------------------------------------------------- |
| Owner (sole user)            | Reads the digest in under 10 minutes; trusts it to surface the day's events and the best individual takes |
| Followed authors / platforms | Their terms of service and rate limits; content is fetched for personal reading only, never republished   |
| Operator (same person)       | Low maintenance; a broken source must not break the digest; cost near zero                                |

## Functional requirements

### FR1 Source coverage

- FR1.1 Ingest from mainstream outlets and vendor blogs via RSS/Atom (OpenAI, Anthropic, Google DeepMind, Meta AI, NVIDIA, Hugging Face, Cloudflare, arXiv cs.AI/cs.LG/cs.DC, The Verge AI, MIT Tech Review AI, etc.).
- FR1.2 Ingest posts from **followed individual accounts** on social platforms: Xiaohongshu (RedNote), X/Twitter, Bluesky, WeChat Official Accounts (公众号), Zhihu, Bilibili, YouTube, Substack newsletters, personal blogs.
- FR1.3 Ingest community discussion: Hacker News (stories + top comments), Reddit (r/MachineLearning, r/LocalLLaMA; top posts; the public feed carries no comments), Xiaohongshu top comments on followed posts.
- FR1.4 Sources are configuration, not code: each source has a kind (rss, hn, reddit, bluesky, youtube, xiaohongshu, x), an identifier, an enabled flag, a weight between 0.2 and 2.0 and a category (media, vendor, author, community, paper). The owner can add, disable or reweight sources without a deploy (`aid sources`, `PATCH /api/sources/{id}`).
- FR1.5 A failing source is skipped and reported in the digest footer; it never aborts the run.

### FR2 Normalisation and deduplication

- FR2.1 Every item has a stable identity derived from its canonical URL (tracking parameters removed, platform-specific IDs preserved). The same article seen on two days or via two sources is one item.
- FR2.2 Items already sent are never sent again, across days.
- FR2.3 Near-duplicates (same story, different outlet or re-post) are detected by embedding similarity and collapsed into one event (see FR3).

### FR3 Event clustering

- FR3.1 Items published within a rolling window (default 72 h) are grouped into **events** by semantic similarity. An event has a title, a one-paragraph summary, the member items, and the list of sources covering it.
- FR3.2 Within an event, items are labelled by kind: primary source (announcement, paper, release), media coverage, individual commentary, community discussion.
- FR3.3 The digest presents events, not raw items. Each event shows the primary source first, then the best individual takes and top comments.

### FR4 Topic tracking across days

- FR4.1 An event that continues to receive new items on later days is a **thread**. The digest shows "Day N of this story", what is new today, and links to earlier digests.
- FR4.2 The owner can pin a topic (e.g. "GPT-6 launch", "H200 export controls") and receive a dedicated section until unpinned or inactive for 7 days.
- FR4.3 Thread state (events, membership, day count) persists in a database, not in agent memory.

### FR5 Ranking and selection

- FR5.1 Relevance is scored by an LLM against the owner's interest profile (topics, keywords, followed authors, blocked sources, technical depth).
- FR5.2 Individual-voice content is scored separately from media so it is not crowded out; the digest guarantees a minimum share (default 30 %) for author posts and comments when available.
- FR5.3 Comments are selected by engagement (likes/upvotes) and by LLM judgement of whether they add a distinct viewpoint, not just agreement.
- FR5.4 The ranking model also tags every event with one or two topics from a fixed taxonomy. The final recommendation score is the product of the model's relevance, the strongest source weight, the owner's learned weight for the event's topics, a boost for coverage by several independent sources, a freshness decay and a penalty for events with almost no body text.
- FR5.5 An event that repeats a story already ranked higher (embedding similarity above a threshold) is held back, so the digest does not carry one story twice.
- FR5.6 The digest is organised by category (models, research, infrastructure, agents and tools, business and products, policy and safety, other), taken from each event's main topic. Every category gets its best events up to a per-category budget (10, 15 or 20 by digest style), so no single category fills the digest; a category with less news shows what it has.
- FR5.7 Before the email is composed, one editor pass over the selection merges entries that report the same story (including the same news in English and Chinese) and drops stories that only repeat what was sent in the last few days. Within a category, forum-only entries (Reddit, Hacker News) beyond three and entries from one source beyond two only fill places that nothing better takes.

### FR6 Summarisation and digest composition

- FR6.1 For each selected event the LLM produces: a neutral summary, the key claims, and a short "different takes" section quoting individual posts and comments with attribution and links.
- FR6.2 Digest style (brief, balanced, deep) controls the summary length and the number of events per category.
- FR6.3 The email is grouped by category (FR5.6); a story that has run for more than one day carries a "Day N of this story" badge.
- FR6.4 Each event is summarised in the language of its sources: English sources in English, Chinese sources in Chinese, any other language in English; quotes stay in their original language.
- FR6.5 Headlines and summaries follow the owner's humanizer guide: sentence-case headlines that state what happened, summaries that name who did what and add facts beyond the headline, no dashes, no stock AI phrasing, and no description of the article instead of the news.

### FR7 Delivery and interaction

- FR7.1 One email per day at the owner's local delivery hour; manual trigger available.
- FR7.2 Each event in the email has feedback links (more like this / less like this / mute source). Feedback adjusts source weights and interest keywords.
- FR7.2a "More like this" and "less like this" also move the owner's weight for the event's topics.
- FR7.2b The foot of each email carries at most two multiple-choice questions: which of the day's topics to see more of, and (alternating by day) whether the digest was too long or too short, or how technical the summaries should be. One tap answers; each question accepts one answer per digest.
- FR7.2c The default layout is compact: each event is a title and a summary of one or two sentences, and taste is collected by the quiz alone, which keeps a digest of sixty events below the size at which mail clients clip a message. Key claims, quoted takes and the per-event feedback links belong to the full layout (`DIGEST_COMPACT = False`).
- FR7.3 (Not implemented in this version.) A chat interface that lets the owner change preferences, ask "why was this included" and see a thread's history through structured tool calls. Today preferences change through the email questions, `aid prefs` and `PUT /api/preferences`, and the rationale of each placed event is stored with the digest.

## Quality attributes (measurable)

- QA1 Digest generation completes within 15 minutes of the scheduled time for up to 60 sources / 2 000 raw items per day.
- QA2 A source outage affects only that source; the run succeeds with ≥ 1 healthy source.
- QA3 Duplicate rate in the sent digest (same URL or same event twice) is 0; near-duplicate rate (same story as separate events) under 5 % measured over a week.
- QA4 Monthly running cost under USD 10 on the free/low tiers of the chosen platforms (excluding any paid social API).
- QA5 All LLM outputs are schema-validated; a malformed response is retried, never emailed.
- QA6 Secrets (API keys, platform cookies) are never committed; they live in platform secret stores.

## Qualitative requirements

- QR1 Content is for the owner's reading only; nothing is republished or shared, and fetch rates stay well under each platform's limits.
- QR2 The system degrades gracefully: when social bridges are blocked, the digest still ships with RSS sources.
- QR3 Code follows the project style guide (`skill.md`, kept outside the repository).

## Source feasibility (as of 2026-11)

| Source                               | Access path                                                               | Feasibility    | Notes                                                          |
| ------------------------------------ | ------------------------------------------------------------------------- | -------------- | -------------------------------------------------------------- |
| Vendor blogs, media, arXiv, Substack | RSS/Atom                                                                  | Easy           | Direct fetch                                                   |
| Hacker News + comments               | Algolia API / Firebase API                                                | Easy           | Public, no key                                                 |
| Reddit                               | Public Atom feed (posts only); OAuth API for scores and comments          | Easy–medium    | Anonymous JSON is refused; comments need a free app id         |
| Bluesky                              | AT Protocol public API                                                    | Easy           | Public feeds, no key                                           |
| YouTube channels                     | RSS feed per channel + transcript API                                     | Easy–medium    | Transcripts need a helper                                      |
| X / Twitter                          | Official API (paid, USD 100+/month) or RSSHub route with logged-in cookie | Medium–fragile | Scraping breaks ToS; use a throwaway account                   |
| Xiaohongshu                          | No public API. RSSHub route with cookie, or a headless-browser scraper    | Hard–fragile   | Strong anti-bot; comments need extra requests; expect breakage |
| WeChat Official Accounts             | RSSHub / wewe-rss (needs a Reading account cookie)                        | Medium         | Common community solution                                      |
| Zhihu, Bilibili                      | RSSHub routes                                                             | Medium         | Some need cookies                                              |

Decision: Xiaohongshu and X are read with a logged-in browser session (Playwright), isolated in
their own adapters and switched off until a cookie is configured; a failure there never stops the
digest (FR1.5, QR2). Because an automated session on a server risks the account, the planned
direction is to read the Xiaohongshu feed on the owner's own computer and push the notes to the API.
