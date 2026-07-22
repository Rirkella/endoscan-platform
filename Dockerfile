# EndoScan heavy-job runtime. It runs `python -m endoscan_jobs <command>`
# locally or on a controlled worker using the same lockfile as CI.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    ENDOSCAN_DATA_ROOT=/data

# Copy manifests first for layer caching, then only the installable job runtime packages.
COPY pyproject.toml uv.lock README.md ./
COPY packages/ ./packages/

# Scope installation to the job package and its workspace dependencies. API,
# frontend, notebook, and development dependencies are intentionally absent.
RUN uv sync --frozen --no-dev --package endoscan-jobs

RUN mkdir -p /data

ENTRYPOINT ["/app/.venv/bin/python", "-m", "endoscan_jobs"]
CMD ["--help"]
