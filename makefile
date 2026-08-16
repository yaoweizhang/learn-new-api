.PHONY: test test-s% run-s% all clean

CHAPTERS := $(wildcard s*_*)

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
	find . -maxdepth 3 -type f \( -name "*.db" -o -name "*.sqlite" -o -name "*.sqlite3" \) -delete
	-rm -f /tmp/learn-new-api-*.db 2>/dev/null || true
	@echo "clean: __pycache__, .pytest_cache, *.db litter"