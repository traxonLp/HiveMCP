# syntax=docker/dockerfile:1
#
# Build from the repository root:
#   docker build -f deploy/Dockerfile -t hivemcp:dev .

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
# Test
# --------------------------------------------------------------------------- #
# Not part of the runtime chain: `docker build --target test` runs the suite, a plain
# build skips this stage entirely and costs nothing.
#
# The point of testing in the image rather than on the host is that this is the exact
# interpreter and the exact dependency set that production runs. Two bugs so far were
# import-level breakage that only appeared inside the container — the kind a host venv
# with slightly different versions can hide. It also means the suite is runnable without
# a local Python 3.11+ at all, which is the difference between tests that run and tests
# that merely exist.
FROM builder AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        pytest'>=8.3' pytest-asyncio'>=0.24' pyyaml'>=6.0'

COPY tests ./tests
# The Kubernetes manifests are checked for internal consistency — that the Deployment
# mounts the paths the ConfigMap names, that the ingress host matches HIVE_PUBLIC_URL,
# that fsGroup matches the image's uid. Cheap to verify, and each of those mismatches
# fails at deploy time in a way that reads like something else.
COPY deploy/k8s ./deploy/k8s

# pytest is the entrypoint rather than a RUN step. Running it during the build would
# turn every red test into a *build* failure: the output arrives wrapped in BuildKit's
# step prefixes, nothing is inspectable afterwards because no image was produced, and
# there is no way to re-run a single test without invalidating the layer. As an
# entrypoint the exit code still propagates to `make test`, and arguments pass straight
# through — `make test ARGS="-k templates -x"`.
ENTRYPOINT ["/opt/venv/bin/python", "-m", "pytest"]
CMD ["-q"]

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
# Both mount points are created here and owned by the runtime user. That ownership is
# what Docker copies into an empty named volume on first mount — a path that does not
# exist in the image gets a mount point owned by root instead, and the container, running
# as uid 10001, then fails to write with a permission error that only surfaces the first
# time someone uploads something.
RUN groupadd --gid 10001 hive \
    && useradd --uid 10001 --gid hive --no-create-home --shell /usr/sbin/nologin hive \
    && mkdir -p /data /templates \
    && chown -R hive:hive /data /templates

COPY --from=builder --chown=root:root /opt/venv /opt/venv

USER hive
WORKDIR /app
VOLUME ["/data", "/templates"]
EXPOSE 8080

# No curl in the slim image, and adding it just for a healthcheck would widen the
# attack surface for no benefit.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status==200 else 1)"]

ENTRYPOINT ["python", "-m", "hivemcp"]
