# Makefile — developer shortcuts for the SSC PACS stack.
# Run from the repo root.  Assumes `conda activate pacs` is active.

WEBAPP := stanford-stroke-pacs/web-app
PYTHON    := python
NPM       := npm

.PHONY: install-dev lint test test-backend test-frontend build

install-dev:  ## Install all dev dependencies (Python + Node)
	pip install -r $(WEBAPP)/requirements.txt -r $(WEBAPP)/requirements-dev.txt
	cd $(WEBAPP) && $(NPM) ci
	pre-commit install

lint:  ## Run all linters (ruff + pre-commit hooks)
	ruff check $(WEBAPP)/ --config $(WEBAPP)/pyproject.toml
	cd $(WEBAPP) && $(NPM) run build --dry-run 2>/dev/null || true

test: test-backend test-frontend  ## Run all tests

test-backend:  ## Run pytest against the web-app backend
	cd $(WEBAPP) && $(PYTHON) -m pytest tests/ \
		--cov=. --cov-report=term-missing --tb=short -v

test-frontend:  ## Run vitest against the web-app frontend
	cd $(WEBAPP) && npx vitest run

build:  ## Build the production frontend bundle
	cd $(WEBAPP) && $(NPM) ci && $(NPM) run build

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
