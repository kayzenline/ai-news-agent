.PHONY: install run test demo check-llm
install:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
run:
	.venv/bin/uvicorn app.main:app --reload --port 8000
test:
	.venv/bin/pytest -q
check-llm:
	.venv/bin/python -m app.llm.check
demo:
	LLM_PROVIDER=fake .venv/bin/uvicorn app.main:app --port 8000
