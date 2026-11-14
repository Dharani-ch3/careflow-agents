.PHONY: install lint format typecheck test evals check demo api mcp clean

install:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check .
	ruff format --check .

format:
	ruff check --fix .
	ruff format .

typecheck:
	mypy

test:
	pytest

evals:
	careflow evals

# Everything CI runs, in the same order.
check: lint typecheck test evals

demo:
	careflow demo

api:
	uvicorn careflow.api:app --reload

mcp:
	careflow-mcp

clean:
	rm -rf data .pytest_cache .mypy_cache .ruff_cache build dist
