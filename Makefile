# Everything runs inside Docker so a clean machine only needs Docker + make.
# Set LOCAL=1 to run with your own Python instead (needs `pip install -r requirements.txt`
# and DATABASE_URL pointing at Postgres, default postgresql://rotto:rotto@localhost:5432/rotto).

SHELL := /bin/bash
COMPOSE := docker compose
FIXTURE_PATH ?= tests/fixtures/lifecycle.json
FIXTURE_ABS := $(abspath $(FIXTURE_PATH))
FIXTURE_DIR := $(dir $(FIXTURE_ABS))
FIXTURE_FILE := $(notdir $(FIXTURE_ABS))

# Load .env if present (proxy credentials live there locally; never committed).
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: crawl test db build export unit probe clean-db

db:
	$(COMPOSE) up -d --wait db

build:
	$(COMPOSE) build crawler

ifeq ($(LOCAL),1)
crawl:
	python -m crawler crawl
test:
	FIXTURE_PATH=$(FIXTURE_ABS) python -m crawler.lifecycle_test
export:
	python -m crawler export
unit:
	python -m unittest discover -s tests -p 'test_*.py' -v
else
crawl: db build
	mkdir -p output logs debug
	$(COMPOSE) run --rm crawler python -m crawler crawl

test: db build
	$(COMPOSE) run --rm -v "$(FIXTURE_DIR):/fixture:ro" -e FIXTURE_PATH=/fixture/$(FIXTURE_FILE) \
		crawler python -m crawler.lifecycle_test

export: db build
	$(COMPOSE) run --rm crawler python -m crawler export

unit: build
	$(COMPOSE) run --rm --no-deps crawler python -m unittest discover -s tests -p 'test_*.py' -v
endif

# Fetch one URL through the proxy and show what the parser extracts (selector debugging).
# make probe SOURCE=reed URL=https://www.reed.co.uk/jobs/... KIND=detail
probe: build
	$(COMPOSE) run --rm --no-deps crawler python -m scripts.probe $(SOURCE) "$(URL)" $(KIND)

clean-db:
	$(COMPOSE) down -v
