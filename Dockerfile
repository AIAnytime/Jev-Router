FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY jev_router ./jev_router
RUN pip install ".[postgres]"

COPY config ./config
COPY evals ./evals

RUN useradd --create-home --uid 1000 jev && mkdir -p /app/data && chown -R jev /app/data
USER jev

ENV DATABASE_URL=sqlite+aiosqlite:////app/data/jev_router.db \
    JEV_ROUTER_CONFIG_DIR=/app/config

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')" || exit 1

CMD ["jev-router", "serve", "--host", "0.0.0.0", "--port", "8000"]
