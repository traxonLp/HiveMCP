# syntax=docker/dockerfile:1
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t hivemcp:0.1.0 .

# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS builder

# Floating tag on purpose for now: pin to a digest once the image is built in CI.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# Dependencies are installed in their own layer, keyed only on pyproject.toml. Source
# changes therefore never re-run the dependency download, which is what makes rebuilds
# cheap on a slow connection. The empty package is a stand-in that lets the project
# metadata resolve; the real code arrives in the next layer.
#
# --python is passed explicitly rather than relying on VIRTUAL_ENV, which uv only
# honours in some invocation modes and fails confusingly when it does not.
# Everything resolves to a manylinux wheel (lxml and Pillow included), so no compiler
# is needed here and none ends up in the runtime image.
COPY pyproject.toml README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    mkdir -p hivemcp && touch hivemcp/__init__.py \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python .

# Now the real source. --no-deps keeps this offline: everything it needs is already in
# the venv from the layer above, so this step costs a second or two even with no network.
COPY hivemcp ./hivemcp
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python --no-deps --force-reinstall .

# --------------------------------------------------------------------------- #
# Runtime
# --------------------------------------------------------------------------- #
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HIVE_DATA_DIR=/data \
    HIVE_PORT=8080

# UID is fixed so the PVC's fsGroup can be set to match it in the Kubernetes manifest.
RUN groupadd --gid 10001 hive \
    && useradd --uid 10001 --gid hive --no-create-home --shell /usr/sbin/nologin hive \
    && mkdir -p /data \
    && chown -R hive:hive /data

COPY --from=builder --chown=root:root /opt/venv /opt/venv

USER hive
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080

# No curl in the slim image, and adding it just for a healthcheck would widen the
# attack surface for no benefit.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "-m", "hivemcp"]
