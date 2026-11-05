# Deployment

One small Linux server (1 vCPU, 2 GB RAM is enough; the embedding model needs about
1 GB of RAM while a run is in progress). Everything runs in Docker; the host only
needs Docker, systemd and Caddy.

## 1. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"   # log out and in again
docker compose version             # Compose v2 is bundled with Docker
```

## 2. Clone and configure

```bash
sudo mkdir -p /opt/ai-infra-daily && sudo chown "$USER" /opt/ai-infra-daily
git clone <repository-url> /opt/ai-infra-daily
cd /opt/ai-infra-daily
cp .env.example .env
```

Edit `.env`. Every variable without a default is required; the process refuses to start
when one is missing. The ones that matter on a server:

| Variable | Value |
| --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API key (summaries and ranking) |
| `OWNER_EMAIL`, `RESEND_API_KEY`, `EMAIL_FROM` | Where the digest goes and how it is sent |
| `TIMEZONE`, `DELIVERY_HOUR` | Owner's timezone and the local hour to send at |
| `PUBLIC_BASE_URL` | `https://<your-domain>`; feedback links in the email point here |
| `OWNER_TOKEN` | A long random string; bearer token for the owner API |
| `FEEDBACK_SECRET` | A long random string; signs the feedback links |
| `XIAOHONGSHU_COOKIE`, `X_COOKIE` | Logged-in cookie headers; empty switches those sources off |

Generate secrets with `openssl rand -hex 32`. `DATABASE_PATH`, `API_HOST` and `API_PORT`
are set by `deploy/compose.yaml` and can stay at their example values.

The SQLite database, the downloaded embedding model and dry-run previews live in
`./data`, which Compose mounts into both containers. Back up that directory.

## 3. Build and initialise

```bash
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml run --rm digest aid migrate
docker compose -f deploy/compose.yaml run --rm digest aid seed
docker compose -f deploy/compose.yaml run --rm digest aid sources list
```

The first build installs Chromium for the browser-rendered sources, so it takes a few
minutes. `aid seed` inserts the default sources once; later edits through the CLI or
the API are never overwritten.

## 4. First run

```bash
docker compose -f deploy/compose.yaml run --rm digest aid run --force --dry-run
```

`--force` ignores the delivery hour and `--dry-run` writes the digest to
`data/digest-<date>.html` instead of emailing it. The first run downloads the embedding
model (about 2 GB for `BAAI/bge-m3`) into `data/models`. Open the HTML file, and when it
looks right send a real one:

```bash
docker compose -f deploy/compose.yaml run --rm digest aid run --force
docker compose -f deploy/compose.yaml run --rm digest aid digests list
```

## 5. Schedule the hourly run

```bash
sudo cp deploy/aid-digest.service deploy/aid-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aid-digest.timer
systemctl list-timers aid-digest.timer
journalctl -u aid-digest.service -n 50 --no-pager
```

The timer fires every hour. `aid run` compares the current hour in the owner's timezone
with `DELIVERY_HOUR` and the date of the last digest, so it sends at most one digest per
local day and exits at once otherwise. Change the hour with `aid prefs set-hour 7`; no
restart is needed.

## 6. The HTTP API behind Caddy

The API serves the feedback links in every email and the owner endpoints (sources,
preferences, digests, manual runs). It listens on `127.0.0.1:8000` only; Caddy
terminates HTTPS in front of it.

```bash
sudo cp deploy/aid-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aid-api.service
curl http://127.0.0.1:8000/api/health
```

Install Caddy (<https://caddyserver.com/docs/install>), point your domain's DNS at the
server, then:

```bash
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/aid.example.com/<your-domain>/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl https://<your-domain>/api/health
curl -H "Authorization: Bearer $OWNER_TOKEN" https://<your-domain>/api/sources
```

Owner endpoints (all need the bearer token):

| Method and path | Purpose |
| --- | --- |
| `GET /api/sources`, `PATCH /api/sources/{id}` | List sources; `{"enabled": false}` or `{"weight": 1.5}` |
| `GET /api/digests`, `GET /api/digests/{id}` | Recent digests; the HTML of one |
| `GET /api/threads?pinned=1` | Active or pinned threads |
| `GET /api/preferences`, `PUT /api/preferences` | Profile and interests; PUT takes `{"profile": ..., "interests": ...}` |
| `POST /api/runs` | `{"force": true, "dry_run": false}` starts a run in the background and returns its id |

## 7. Cookie refresh

Xiaohongshu and X are fetched with a logged-in browser session. Cookies expire every few
weeks; when `aid sources list` shows those sources failing with `blocked` or
`not_configured`, refresh them:

1. Log in to the platform in a desktop browser, open the developer tools, and copy the
   `Cookie` request header of any request to the site (see `docs/xiaohongshu.md` and
   `docs/x.md` for which cookies matter).
2. Paste it into `.env` as `XIAOHONGSHU_COOKIE=...` or `X_COOKIE=...` on one line.
3. Restart the API so it picks up the new value; the timer reads `.env` on every run.

```bash
sudo systemctl restart aid-api.service
docker compose -f deploy/compose.yaml run --rm digest aid stage fetch
```

## 8. Updating

```bash
cd /opt/ai-infra-daily
git pull
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml run --rm digest aid migrate
sudo systemctl restart aid-api.service
```

## Troubleshooting

- `aid run` prints `outside delivery hour` or `already sent today`: expected outside
  the delivery hour; use `--force` to run anyway.
- `delivery failed: not_configured`: `RESEND_API_KEY` is empty.
- A source keeps failing: it is reported in the digest footer and never stops the run
  (`aid sources disable <id>` silences it).
- Logs are JSON lines on stderr: `journalctl -u aid-digest.service` for runs,
  `docker compose -f deploy/compose.yaml logs api` for the API. Every line of a run
  carries its `run_id`; the `runs` table records the last stage reached.
