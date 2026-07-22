PYTHON ?= uv run python

.PHONY: setup dev api web test test-workflows test-api test-frontend lint format typecheck build demo demo-tr demo-dna-damage verify

setup dev api web test test-workflows test-api test-frontend lint format typecheck build demo demo-tr demo-dna-damage verify:
	$(PYTHON) scripts/project.py $@
