# Convenience targets. Everything works with plain `uv` too -- see README.
.DEFAULT_GOAL := help
.PHONY: help install test test-fast lint fmt smoke ingest docker-build docker-test clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

install: ## Sync the virtualenv and stage the Delta JARs
	uv sync --all-groups
	uv run python scripts/warm_delta_jars.py

test: ## Run the full test suite
	uv run pytest -q

test-fast: ## Skip the tests that need a JVM
	uv run pytest -q -m "not spark"

lint: ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-fix lint and formatting
	uv run ruff check --fix .
	uv run ruff format .

smoke: ## Phase 0 acceptance: Spark + Delta round-trip
	uv run registry smoke

ingest: ## Pull the live FDA list into bronze (needs network)
	uv run registry ingest-fda-list --verbose

docker-build: ## Build the dev image
	docker compose build registry

docker-test: ## Run the suite inside the container
	docker compose run --rm test

clean: ## Remove local build/test artefacts (leaves ./lakehouse alone)
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage metastore_db derby.log spark-warehouse
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
