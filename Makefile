.PHONY: sync test lint fmt precommit build-docker run

sync: ## uv lock + uv sync with dev deps.
	uv sync --group dev

test: ## Run the pytest suite.
	uv run pytest

lint: ## ruff check + ruff format --check + mypy on src/ and tests/.
	uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests

fmt: ## Apply ruff fixes and formatting in place.
	uv run ruff check --fix src tests && uv run ruff format src tests

precommit: ## Run all pre-commit hooks against every file.
	uv run pre-commit run --all-files

build-docker: ## Build the node-stats-mcp docker image locally.
	docker build -t node-stats-mcp:local .

run: ## Run the MCP server locally over streamable-HTTP on :8080.
	uv run node-stats-mcp
