# Build from the repository root: docker build -f deploy/Dockerfile -t aid .
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PATH="/srv/aid/.venv/bin:$PATH"

WORKDIR /srv/aid

# Dependencies and the browser first so source edits do not invalidate these layers.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN playwright install --with-deps chromium

COPY . .
RUN uv sync --frozen --no-dev

# Runs as an ordinary user whose id matches the host user that owns ./data.
ARG AID_UID=1000
RUN useradd --uid "${AID_UID}" --create-home --shell /usr/sbin/nologin aid \
    && mkdir -p /srv/aid/data && chown aid /srv/aid/data
USER aid

VOLUME ["/srv/aid/data"]
EXPOSE 8000

CMD ["aid", "--help"]
