# Makefile — developer shortcuts for the SSC PACS stack.
# Run from the repo root.  Assumes `conda activate ssc-pacs` is active.

WEBAPP    := stanford-stroke-pacs/web-app
INGESTION := stanford-stroke-pacs/image_ingestion_protocols
SCRIPTS   := stanford-stroke-pacs/scripts
PYTHON    := python
NPM       := npm

# Fail with an actionable message when a tool from the conda env is missing.
define require
@command -v $(1) >/dev/null 2>&1 || { \
	echo "error: '$(1)' not found — run 'conda activate ssc-pacs' first (then 'make install-dev' if still missing)" >&2; \
	exit 1; }
endef

.PHONY: install-dev lint test test-backend test-frontend test-ingestion build help

install-dev:  ## Install all dev dependencies (Python + Node) + pre-commit hooks
	pip install -r $(WEBAPP)/requirements.txt -r $(WEBAPP)/requirements-dev.txt
	cd $(WEBAPP) && $(NPM) ci
	pre-commit install

# ruff's first-party import detection is cwd-sensitive: each Python surface is
# linted from the directory whose modules are first-party (web-app's db/auth/
# common…, the ingestion package's flat modules) so I001 import sorting matches
# the pre-commit hook. scripts/ has no sibling first-party modules and lints
# fine from the repo root. web-app/pyproject.toml is the single ruff ruleset.
lint:  ## Run all linters (ruff: web-app + scripts + ingestion; eslint: frontend)
	$(call require,ruff)
	cd $(WEBAPP) && ruff check .
	ruff check $(SCRIPTS) --config $(WEBAPP)/pyproject.toml
	cd $(INGESTION) && ruff check . --config ../web-app/pyproject.toml
	cd $(WEBAPP) && $(NPM) run lint

test: test-backend test-frontend test-ingestion  ## Run all tests

test-backend:  ## Run pytest against the web-app backend (needs local Postgres)
	$(call require,pytest)
	cd $(WEBAPP) && $(PYTHON) -m pytest tests/ \
		--cov=. --cov-report=term-missing --tb=short -v

test-frontend:  ## Run vitest against the web-app frontend
	cd $(WEBAPP) && npx vitest run

test-ingestion:  ## Run the ingestion-protocol suite (DB-free; e2e gated on SSC_INGEST_AUDIT=1)
	$(call require,pytest)
	cd $(INGESTION) && $(PYTHON) -m pytest tests/ --tb=short

build:  ## Build the production frontend bundle
	cd $(WEBAPP) && $(NPM) ci && $(NPM) run build

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
