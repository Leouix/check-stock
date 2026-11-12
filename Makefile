PYTHON ?= .venv/bin/python
PYTEST ?= .venv/bin/pytest

.PHONY: db seed reset run test verify up down

db:
	docker compose up -d db

seed:
	$(PYTHON) -m app.seed

reset:
	$(PYTHON) -m app.seed --reset

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test:
	$(PYTEST)

verify:
	PYTHONPATH=. $(PYTHON) scripts/verify_flow.py

up:
	docker compose up --build

down:
	docker compose down
