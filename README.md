# findr

FastAPI-based project.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Configuration

Copy `.env.example` to `.env` and fill in real values before running:

```bash
cp .env.example .env
```

## Run

```bash
uv run fastapi dev src/findr/app.py
```

## Tests

```bash
uv run pytest
```
