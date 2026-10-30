# Stage 1: resolve and install dependencies into /app/.venv
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS build
# https://docs.astral.sh/uv/guides/integration/docker
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra agent --no-install-project

COPY src/ src/
COPY projects/ projects/
COPY logging.yml README.md LICENSE.txt ./
RUN uv sync --frozen --no-dev --extra agent

# Stage 2: runtime image (same Python as the uv image, no uv)
FROM python:3.13-slim-trixie AS runtime
RUN groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home app

WORKDIR /app
COPY --from=build --chown=app:app /app /app
RUN mkdir -p /app/data /cache && chown app:app /app/data /cache

ENV PYTHONUNBUFFERED=1 \
    SERVER_PORT=8000 \
    DATA_DIR=/app/data \
    FASTEMBED_CACHE_PATH=/cache/fastembed \
    HF_HOME=/cache/hf \
    PATH=/app/.venv/bin:$PATH

USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=10m --retries=3 CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"SERVER_PORT\",\"8000\")}/readyz', timeout=8)"
CMD ["sh", "-c", "exec uvicorn sparql_llm.agent.main:app --host 0.0.0.0 --port ${SERVER_PORT} --log-config logging.yml --timeout-graceful-shutdown 10"]
