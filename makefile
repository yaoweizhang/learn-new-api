.PHONY: test test-s% run-s% all clean

CHAPTERS := $(wildcard s*_*)
PORTS    := $(shell seq 8001 8017)

test:
	pytest tests/ -v

# Run a single chapter's tests: make test-s05
test-s%:
	pytest tests/test_s$*.py -v

# Run a single chapter's server: PORT=8005 make run-s05
run-s%:
	cd s$* && PORT=$(PORT) python code.py

all:
	@for d in $(CHAPTERS); do echo "=== $$d ==="; ls $$d; done

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	rm -rf .pytest_cache