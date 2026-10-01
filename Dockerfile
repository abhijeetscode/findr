# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies first, as their own cached layer — only re-runs when
# pyproject.toml/uv.lock change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now the project itself.
COPY src/ ./src/
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm AS runtime

# System libraries for unstructured's PDF pipeline (specs/upload-chunking.md
# §10): libxcb1/libgl1/libglib2.0-0 for OpenCV (partition_pdf won't even
# import without them), poppler-utils to render PDF pages, tesseract-ocr for
# OCR (English data included).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libxcb1 libgl1 libglib2.0-0 poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# --create-home: libraries used by unstructured (fontconfig, matplotlib)
# write caches under ~/.cache.
RUN groupadd --system findr && useradd --system --create-home --gid findr findr
# Mount points for the uploads and model-cache volumes (docker-compose.yml),
# created and chowned here so the named volumes inherit findr ownership —
# otherwise they'd mount root-owned and unwritable by the app user.
RUN mkdir -p /data/uploads /data/hf-cache && chown -R findr:findr /data
# The findr user is a --system user with no home directory, so point the
# Hugging Face cache at the volume. The embedding model (~1.2GB) downloads
# there on first startup and is reused across restarts and rebuilds.
ENV HF_HOME=/data/hf-cache
# unstructured uses Numba, which caches compiled functions next to the source
# in site-packages by default — not writable by the findr user.
ENV NUMBA_CACHE_DIR=/tmp/numba-cache
WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src
ENV PATH="/app/.venv/bin:${PATH}"

USER findr
EXPOSE 8000

CMD ["uvicorn", "findr.adapters.inbound.http.app:app", "--host", "0.0.0.0", "--port", "8000"]
