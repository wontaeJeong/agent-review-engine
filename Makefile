.PHONY: install test lint format-check migrate serve worker

install:
	uv sync --extra dev

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

format-check:
	uv run ruff format --check src tests

migrate:
	uv run alembic upgrade head

serve:
	uv run uvicorn agent_review_engine.app:app --host 0.0.0.0 --port 8080

worker:
	uv run python -m agent_review_engine.worker
