COMPOSE := docker compose -f deploy/docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help build up down logs smoke shell test lint clean owui env check-secrets

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from the template and generate a signing key
	@test -f .env && { echo ".env already exists, leaving it alone"; exit 0; } || true
	@cp .env.example .env
	@key=$$(python3 -c "import secrets; print(secrets.token_urlsafe(32))"); \
	  sed -i.bak "s|^HIVE_SIGNING_KEY=.*|HIVE_SIGNING_KEY=$$key|" .env && rm -f .env.bak
	@echo "Wrote .env with a fresh HIVE_SIGNING_KEY (git-ignored)."
	@echo "Next: add HIVE_OWUI_API_KEY from OpenWebUI (Settings -> Account -> API Keys)."

check-secrets: ## Fail if anything credential-shaped is tracked by git
	@hits=$$(git grep -nIE 'sk-[A-Za-z0-9_-]{16,}' -- ':!*.example' ':!docs/*' || true); \
	if [ -n "$$hits" ]; then \
	  echo "$$hits"; echo; echo "Possible API key in a tracked file. Do not commit."; exit 1; \
	fi
	@hits=$$(git grep -nIE '^[[:space:]]*(HIVE_(AUTH_TOKEN|SIGNING_KEY|OWUI_API_KEY)|WEBUI_SECRET_KEY)[:=][[:space:]]*[^[:space:]#$$]' \
	  -- ':!*.example' ':!docs/*' ':!Makefile' ':!deploy/docker-compose.yml' || true); \
	if [ -n "$$hits" ]; then \
	  echo "$$hits"; echo; echo "Secret assigned in a tracked file. Move it to .env."; exit 1; \
	fi
	@echo "check-secrets: nothing credential-shaped in tracked files"

build: ## Build the container image
	$(COMPOSE) build

up: ## Build and start HiveMCP, then wait until it is healthy
	$(COMPOSE) up --build -d --wait
	@echo
	@echo "HiveMCP is up:  http://localhost:8080/healthz"
	@echo "OpenAPI schema: http://localhost:8080/openapi.json"

owui: ## Start HiveMCP together with an OpenWebUI instance (for the M0 spikes)
	$(COMPOSE) --profile owui up --build -d --wait
	@echo
	@echo "HiveMCP:   http://localhost:8080"
	@echo "OpenWebUI: http://localhost:3000"

smoke: ## Run the smoke test against the running container
	@./deploy/smoke.sh

logs: ## Follow container logs
	$(COMPOSE) logs -f hivemcp

shell: ## Open a shell inside the running container
	$(COMPOSE) exec hivemcp /bin/bash

down: ## Stop everything (volumes are kept)
	$(COMPOSE) --profile owui down

clean: ## Stop everything and delete the volumes
	$(COMPOSE) --profile owui down -v

test: ## Run the test suite on the host
	pytest -q

lint: ## Lint and type-check on the host
	ruff check hivemcp tests
	ruff format --check hivemcp tests
	mypy hivemcp
