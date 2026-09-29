# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

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

FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system findr && useradd --system --gid findr findr
WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY --from=builder /app/src ./src
ENV PATH="/app/.venv/bin:${PATH}"

USER findr
EXPOSE 8000

CMD ["uvicorn", "findr.adapters.inbound.http.app:app", "--host", "0.0.0.0", "--port", "8000"]
