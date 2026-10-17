# syntax=docker/dockerfile:1
# ---- build stage: resolve locked deps into a venv ----
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv==0.9.21
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra anthropic --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --extra anthropic --no-editable

# ---- runtime stage: no build tools, non-root ----
FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime
# Pull in Debian security fixes newer than the pinned base image.
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 LLM_CACHE_DIR=/app/.cache/llm
RUN mkdir -p /app/.cache/llm && chown -R app:app /app/.cache
USER app
EXPOSE 8102
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8102/healthz', timeout=4).status == 200 else 1)"
# SERVICE_ROLE (all | api | worker) selects the surface; SIGTERM -> uvicorn closes HTTP connections within 2 s, then
# the lifespan shutdown checkpoints and requeues in-flight runs within SHUTDOWN_GRACE_S (Cloud Run allows 10 s).
ENV SERVICE_ROLE=all LOG_FORMAT=json
CMD ["uvicorn", "research_assistant.api:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8102", \
     "--timeout-graceful-shutdown", "2", "--no-server-header"]
