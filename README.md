# AI Infra Daily

A personal, single-user service that emails one digest a day about AI infrastructure:
mainstream outlets and vendor blogs for the events, plus the individual voices
(bloggers, Xiaohongshu and X accounts, Bluesky, YouTube, top Hacker News and Reddit
comments) that interpret them. Items are deduplicated, clustered into events, linked
into multi-day threads, ranked against the owner's interests, summarised in the
language of the source (English or Chinese) and composed into one email, grouped by
category. One or two questions at the foot of the email tune tomorrow's ranking; each
link opens a confirmation page, and the answer applies when the owner confirms.

The product requirements are in [`docs/requirements.md`](docs/requirements.md). This is
the Python 3.14 rewrite of a Cloudflare Workers prototype; it runs on one Linux server.

## Architecture

```
            hourly timer                          owner (browser / curl)
                 |                                         |
           aid run (CLI)                           aid serve (FastAPI)
                 |                                         |
                 v                                         v
  +------------------------------------------------------------------+
  |  app.pipeline.run  ->  fetch_all -> ingest -> embed -> cluster   |
  |      -> threads -> rank -> select -> summarise -> compose -> send |
  |  app.api.routes    ->  /quiz, /feedback, /api/{sources,...}      |
  +------------------------------------------------------------------+
                 |  AbstractContext (injected)
                 v
  +-------------------+   +-----------------------------------------+
  | repositories      |   | adapters                                |
  | sources, items,   |   | http (httpx)  llm (DeepSeek)  email      |
  | events, threads,  |   | embeddings (fastembed, local ONNX)       |
  | digests, prefs,   |   | browser (Playwright, cookie sessions)    |
  | runs  -> SQLite   |   | clock                                   |
  +-------------------+   +-----------------------------------------+
```

Dependencies point one way: `settings`, `logging` and `result` are leaves; `models` is
pure data; `adapters` wrap every third party; `repositories` own the SQL; `context`
bundles them; `sources` and `pipeline` contain the logic and see only `AbstractContext`;
`cli` and `api` are the entry points. Expected failures are returned as values
(`OnSuccess[T]`), never raised.

## Ranking and learning

`app/pipeline/recommend.py` holds the scoring as pure functions. The model scores each
event against the interest profile and tags it with topics from a fixed list; the
recommendation score multiplies that relevance by the strongest source weight, the
owner's weight for the topics, a coverage boost (several independent sources), a
freshness decay and a penalty for events with next to no text. Repeats of a story
already ranked higher are held back. Topic weights start neutral and move with the
per-event feedback links and with the one or two multiple-choice questions at the foot
of every email (`app/pipeline/quiz.py`); `aid prefs show` prints them.

The email is organised by category, taken from each event's main topic, and every
category gets its best events up to a per-category budget (10, 15 or 20 by digest
style), so no single one fills the digest. By default an event is a title and a short
summary (`DIGEST_COMPACT` in `app/settings.py`).

## Layout

```
app/
  settings.py        flat configuration read from the environment at import time
  logging.py         JSON logging with scoped fields (run_id, source_id)
  result.py          ServiceError base, OnSuccess[T], is_error / is_success
  models.py          frozen Pydantic models for every domain object
  context.py         AbstractContext, the production Context and default()
  adapters/          http, llm, embeddings, browser, email, clock, database
  repositories/      one class per stored resource; all SQL lives here
  sources/           one adapter per source kind, fetch_all, seeds, registry
  pipeline/          ingest, embed, cluster, threads, rank, recommend, select,
                     summarise, compose, quiz, links, feedback, feedback_token,
                     schedule, run
  api/               FastAPI app, bearer auth, routes
  cli.py             the `aid` command
migrations/          numbered SQL files applied by `aid migrate`
compose.yaml         api and scheduler services
deploy/              Dockerfile, scheduler script, optional systemd units, Caddyfile
docs/                requirements, deployment, per-platform cookie notes
data/                SQLite database, model cache and dry-run previews (git-ignored)
```

## Commands

```bash
uv sync --all-groups                 # install
cp .env.example .env                 # then fill it in
uv run aid migrate                   # create or upgrade the database
uv run aid seed                      # insert the default sources
uv run aid run --force --dry-run     # full pipeline, HTML written to data/
uv run aid run                       # sends only at the delivery hour, once per day
uv run aid stage fetch|embed|cluster|threads
uv run aid sources list|enable|disable|weight
uv run aid prefs show|set-email|set-hour|set-style|set-depth|add-topic|remove-topic|
           add-keyword [--exclude]|remove-keyword|follow|block|unblock
uv run aid digests list
uv run aid threads list|pin <id>|unpin <id>
uv run aid maintain                  # prune old data, daily backup (the scheduler runs it)
uv run aid serve                     # HTTP API on API_HOST:API_PORT
```

Checks: `uv run ruff format --check && uv run ruff check && uv run mypy`.
Server setup is described in [`docs/deployment.md`](docs/deployment.md).
