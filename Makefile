.PHONY: install run test coverage lint format format-check cq ci demo check-llm
install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install ruff pytest-cov
run:
	.venv/bin/uvicorn app.main:app --reload --port 8000
test:
	.venv/bin/pytest -q
coverage:
	.venv/bin/pytest --cov=app --cov-report=term-missing
lint:
	.venv/bin/ruff check app tests
format:
	.venv/bin/ruff format app tests
format-check:
	.venv/bin/ruff format --check app tests
cq: lint format-check
ci: cq coverage
check-llm:
	.venv/bin/python -m app.llm.check
demo:
	LLM_PROVIDER=fake .venv/bin/uvicorn app.main:app --port 8000
