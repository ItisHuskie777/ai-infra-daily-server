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

VOLUME ["/srv/aid/data"]
EXPOSE 8000

CMD ["aid", "--help"]
