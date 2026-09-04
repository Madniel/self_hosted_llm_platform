PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin
HOST ?= 127.0.0.1
PORT ?= 8000
URL ?= http://$(HOST):$(PORT)

.PHONY: help venv install run test lint fmt load load-sweep load-open smoke docker docker-gpu clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## create the virtualenv
	$(PY) -m venv $(VENV)

install: venv ## install runtime + dev dependencies
	$(BIN)/pip install -U pip
	$(BIN)/pip install -r requirements-dev.txt

run: ## start the server (mock backend by default)
	$(BIN)/python -m llmserve

test: ## run the test suite
	$(BIN)/pytest -q

lint: ## ruff check
	$(BIN)/ruff check llmserve loadtest tests

fmt: ## ruff format
	$(BIN)/ruff format llmserve loadtest tests

load: ## single-level load test
	$(BIN)/python -m loadtest --base-url $(URL) --concurrency 16 --duration 20

load-sweep: ## closed-loop concurrency sweep
	$(BIN)/python -m loadtest --base-url $(URL) --concurrency 1,2,4,8,16,32,64 \
		--duration 15 --max-tokens 64 --json results/sweep.json

load-open: ## open-loop arrival-rate sweep (shows admission control shedding)
	$(BIN)/python -m loadtest --base-url $(URL) --rps 5,10,20,40,80 \
		--duration 15 --max-tokens 64 --json results/open.json

smoke: ## one streaming request via curl
	curl -N -s $(URL)/v1/chat/completions \
		-H 'Content-Type: application/json' \
		-d '{"messages":[{"role":"user","content":"hello"}],"max_tokens":24,"stream":true}'

docker: ## build + run the CPU image
	docker compose up --build server

docker-gpu: ## build + run the GPU image
	docker compose --profile gpu up --build server-gpu

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache results **/__pycache__
