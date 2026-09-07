.DEFAULT_GOAL := help
SHELL := /bin/bash
UV := uv --directory bot

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- quality ------------------------------------------------------------------

.PHONY: install
install: ## Sync the locked environment (dev extras included)
	$(UV) sync --frozen --extra dev

.PHONY: lint
lint: ## ruff check + format check + mypy --strict + the INV-07 grep
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests
	$(UV) run mypy
	@$(MAKE) --no-print-directory check-no-hardcoded-models

.PHONY: fmt
fmt: ## Apply ruff formatting and safe fixes
	$(UV) run ruff check --fix src tests
	$(UV) run ruff format src tests

.PHONY: check-no-hardcoded-models
check-no-hardcoded-models: ## INV-07 — no vendor/model string outside config/
	@if grep -rEn "gpt-[0-9]|claude-|gemini-|llama-" bot/src --include="*.py" \
	    | grep -v "src/incidentpilot/config/"; then \
	  echo "::error::hardcoded model name — resolve through LLMRouter roles"; exit 1; \
	fi
	@echo "  no hardcoded model names"

.PHONY: check-config
check-config: ## Assert production configuration invariants
	$(UV) run python -m incidentpilot.config.assert_invariants --environment prod || true

# --- tests --------------------------------------------------------------------

.PHONY: test
test: ## Unit tests with coverage floor
	$(UV) run pytest tests/unit -q --cov=incidentpilot --cov-report=term-missing

.PHONY: test-integration
test-integration: ## Integration tests (needs a live Valkey)
	$(UV) run pytest tests/integration -q -m integration

.PHONY: test-all
test-all: test test-integration ## Everything

# --- local stack --------------------------------------------------------------

.PHONY: up
up: ## Start the stack (works with no .env and no API key)
	docker compose up -d --build
	@echo "  api        http://localhost:$(or $(IP_PORT_API),18000)/healthz"
	@echo "  metrics    http://localhost:$(or $(IP_PORT_API),18000)/metrics"
	@echo "  prometheus http://localhost:$(or $(IP_PORT_PROMETHEUS),19090)"
	@echo "  postgres   localhost:$(or $(IP_PORT_POSTGRES),55432)"
	@echo "  valkey     localhost:$(or $(IP_PORT_VALKEY),56379)"

.PHONY: down
down: ## Stop the stack, keep volumes
	docker compose down

.PHONY: clean
clean: ## Stop the stack and delete volumes
	docker compose down -v

.PHONY: logs
logs: ## Tail the api logs
	docker compose logs -f api

# --- gates --------------------------------------------------------------------

.PHONY: gate-g1
gate-g1: ## G1 — bad bearer 401, good 202 at p99 < 250ms, entry on the stream
	@bash scripts/bench_ingest.sh --n 100 --p99-max-ms 250

.PHONY: gate-g2
gate-g2: ## G2 — migrations round-trip; 40-alert storm to one incident
	@bash scripts/gate_g2.sh

.PHONY: gate-g3
gate-g3: ## G3 — 12 crash points, no duplicate channel on restart
	@bash scripts/gate_g3.sh

.PHONY: gate-g4
gate-g4: ## G4 — 200 messages, 200 rows, zero conversations.history calls
	@bash scripts/gate_g4.sh

.PHONY: relay
relay: ## Run the outbox relay (the only external writer)
	$(UV) run python -m incidentpilot.orchestration.relay

.PHONY: reconciler
reconciler: ## Run the reconciler (the only conversations.history caller)
	$(UV) run python -m incidentpilot.orchestration.reconciler

.PHONY: migrate
migrate: ## Apply migrations to head
	$(UV) run alembic upgrade head

.PHONY: fixtures
fixtures: ## Regenerate the storm fixtures
	$(UV) run python tests/fixtures/make_storm.py
