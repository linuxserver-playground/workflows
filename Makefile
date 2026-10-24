.PHONY: venv check test lint

venv:
	python3 -m venv .venv
	.venv/bin/pip install -q -e '.[dev]'

check:
	./scripts/check.sh

test:
	.venv/bin/python -m pytest -q

lint:
	.venv/bin/shellcheck -x scripts/*.sh .github/actions/*/*.sh
	.venv/bin/actionlint .github/workflows/*.yml
