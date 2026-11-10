# Deployment

One Linux server with Docker. Two long-running containers do everything: `api` serves
the feedback links and the owner API, `scheduler` runs the pipeline at the top of every
hour. Plan for 4 GB of free RAM (the `BAAI/bge-m3` embedding model uses about 3 GB
while a run embeds new items) and 6 GB of disk (image 2.8 GB, model 2.2 GB).

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out and in again
docker compose version             # Compose v2 is bundled with Docker
```

## 2. Clone and configure

```bash
git clone <repository-url> ~/ai-infra-daily-server
cd ~/ai-infra-daily-server
cp .env.example .env && chmod 600 .env
```

Edit `.env`. Every variable without a default must be present; the ones that matter:

| Variable | Value |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API key (ranking and summaries). Empty: no digest can be written |
| `OWNER_EMAIL`, `RESEND_API_KEY`, `EMAIL_FROM` | Where the digest goes and how it is sent |
| `TIMEZONE`, `DELIVERY_HOUR` | Owner's timezone and the local hour to send at |
| `PUBLIC_BASE_URL` | Public address of the API; feedback links in the email point here |
| `API_PUBLISH` | Host address Docker publishes the API on, e.g. `0.0.0.0:8787`; default `127.0.0.1:8000` |
| `OWNER_TOKEN` | A long random string; bearer token for the owner API |
| `FEEDBACK_SECRET` | A long random string; signs the feedback links |
| `XIAOHONGSHU_COOKIE`, `X_COOKIE` | Logged-in cookie headers; empty switches those sources off |

Generate secrets with `openssl rand -hex 32`. `DATABASE_PATH`, `API_HOST` and `API_PORT`
are overridden by `compose.yaml` inside the containers.

`OWNER_EMAIL`, `TIMEZONE`, `DELIVERY_HOUR` and `DIGEST_STYLE` only seed the profile: once
a digest has been sent the profile lives in the database, and `aid prefs` or
`PUT /api/preferences` changes it.

The SQLite database, the downloaded embedding model and dry-run previews live in
`./data`, which Compose mounts into every container. Back up that directory.

## 3. Build and first run

```bash
docker compose build
docker compose run --rm aid aid migrate
docker compose run --rm aid aid seed
docker compose run --rm aid aid run --force --dry-run
```

The first build installs Chromium for the browser-rendered sources and takes a few
minutes. `--force` ignores the delivery hour and `--dry-run` writes the digest to
`data/digest-<date>.html` instead of emailing it. The first run downloads the embedding
model into `data/models`. When the preview looks right, send a real one:

```bash
docker compose run --rm aid aid run --force
docker compose run --rm aid aid digests list
```

Each stage can also be run on its own, which is the quickest way to see where a run
stops: `aid stage fetch`, `aid stage embed`, `aid stage cluster`, `aid stage threads`.

## 4. Start the services

```bash
docker compose up -d
docker compose ps
docker compose logs -f scheduler
curl http://127.0.0.1:8787/api/health      # the port from API_PUBLISH
```

Both containers restart with Docker after a reboot. The scheduler wakes at the top of
every hour; `aid run` compares the current hour in the owner's timezone with the
delivery hour and the date of the last digest, so it sends at most one digest per local
day and exits at once otherwise. Change the hour with
`docker compose run --rm aid aid prefs set-hour 7`; no restart is needed.

Owner endpoints (all need `Authorization: Bearer $OWNER_TOKEN`):

| Method and path | Purpose |
| --- | --- |
| `GET /api/sources`, `PATCH /api/sources/{id}` | List sources; `{"enabled": false}` or `{"weight": 1.5}` |
| `GET /api/digests`, `GET /api/digests/{id}` | Recent digests; the HTML of one |
| `GET /api/threads?pinned=1` | Active or pinned threads |
| `GET /api/preferences`, `PUT /api/preferences` | Profile and interests; PUT takes `{"profile": ..., "interests": ...}` |
| `POST /api/runs` | `{"force": true, "dry_run": false}` starts a run in the background and returns its id |

## 5. HTTPS (optional)

Published directly, the API speaks plain HTTP: feedback links work, but the owner token
would cross the network unencrypted, so call the owner endpoints from the server itself
or put a reverse proxy in front. With a domain pointing at the server, leave
`API_PUBLISH` at its loopback default, install Caddy
(<https://caddyserver.com/docs/install>) and use `deploy/Caddyfile`:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/aid.example.com/<your-domain>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Then set `PUBLIC_BASE_URL=https://<your-domain>` and `docker compose up -d`.

`deploy/aid-digest.timer`, `deploy/aid-digest.service` and `deploy/aid-api.service` are
systemd equivalents of the two Compose services for hosts that prefer systemd to own
the schedule; use them instead of `docker compose up -d`, never together with the
scheduler container.

## 6. Cookie refresh

Xiaohongshu and X are fetched with a logged-in browser session. Cookies expire every few
weeks; when `aid sources list` shows those sources failing with `blocked` or
`not_configured`, refresh them:

1. Log in to the platform in a desktop browser, open the developer tools, and copy the
   `Cookie` request header of any request to the site (see `docs/xiaohongshu.md` and
   `docs/x.md` for which cookies matter).
2. Paste it into `.env` as `XIAOHONGSHU_COOKIE=...` or `X_COOKIE=...` on one line.
3. Recreate the containers so they read the new value, enable the source and check it:

```bash
docker compose up -d
docker compose run --rm aid aid sources enable xiaohongshu-following
docker compose run --rm aid aid stage fetch
```

## 7. Updating

```bash
cd ~/ai-infra-daily-server
git pull
docker compose build
docker compose run --rm aid aid migrate
docker compose up -d
```

## Troubleshooting

- `aid run` prints `before_delivery_hour` or `already_sent_today`: expected; the digest
  is due from the delivery hour until it has gone out that local day, so a failed run is
  retried the next hour, up to three failed attempts a day. A digest that was built but
  not delivered is resent as it is. Use `--force` to run anyway.
- `another run is in progress`: a run already holds the pipeline lease; it expires after
  90 minutes if that run died.
- `every event summary failed`: the LLM is unreachable or `DEEPSEEK_API_KEY` is empty
  or wrong; nothing is sent.
- `delivery failed: not_configured`: `RESEND_API_KEY` is empty.
- A source keeps failing: it is reported in the digest footer and never stops the run
  (`aid sources disable <id>` silences it).
- Logs are JSON lines on stderr: `docker compose logs scheduler` for runs,
  `docker compose logs api` for the API. Every line of a run carries its `run_id`; the
  `runs` table records the last stage reached.
