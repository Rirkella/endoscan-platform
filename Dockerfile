# EndoScan heavy-job runtime — reproducible image used locally AND on the future
# self-hosted runner. Runs `python -m endoscan_jobs run <job> <target>`.
#
# Built from the SAME uv.lock the CI uses (single source of truth). Installs only the
# runtime workspace (endoscan-core + endoscan-jobs incl. rdkit/scikit-learn/requests/
# openpyxl) — NOT the dev/notebook tooling (ruff/pytest/nbformat/h5py/shap/dvc).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    ENDOSCAN_DATA_ROOT=/data

# Workspace manifests + lock first (layer cache); then the package sources.
COPY pyproject.toml uv.lock README.md ./
COPY packages/ ./packages/
# Forward-compat: the later `build` job imports these (top-level, like the tests do).
COPY pipelines/ ./pipelines/
COPY agent/ ./agent/
COPY registry/ ./registry/

# Runtime install only (no dev group). uv builds /app/.venv from the frozen lock.
RUN uv sync --frozen --no-dev

# Persistent artifact root is mounted here on the server (-v <disk>:/data).
RUN mkdir -p /data

ENTRYPOINT ["/app/.venv/bin/python", "-m", "endoscan_jobs"]
CMD ["--help"]
