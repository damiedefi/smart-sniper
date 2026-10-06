.PHONY: install run test sniper lucky backtest forward autopilot report article-kit set-link record replay

install:
	uv venv --python 3.12 .venv
	uv pip install -e ".[dev,article-kit]"

run:
	uv run uvicorn app.server:app --reload --port 8000

test:
	uv run pytest -q

sniper:
	uv run uvicorn app.sniper_server:app --port 8001

lucky:
	uv run python -m app.cli lucky $(if $(CHAIN),--chain $(CHAIN)) $(if $(SOURCE),--source $(SOURCE)) $(if $(WALLETS),--max-wallets $(WALLETS))

backtest:
	uv run python -m app.cli backtest $(if $(WALLETS),--wallets $(WALLETS)) $(if $(SOURCE),--source $(SOURCE)) $(if $(CHAIN),--chain $(CHAIN))

forward:
	uv run python -m app.cli forward $(if $(MINUTES),--minutes $(MINUTES)) $(if $(WALLETS),--wallets $(WALLETS)) $(if $(CHAIN),--chain $(CHAIN))

autopilot:
	uv run python -m app.cli autopilot $(if $(CHAIN),--chain $(CHAIN))

report:
	uv run python -m app.cli report --run $(or $(RUN),latest)

article-kit:
	uv run python -m app.cli article-kit --run $(or $(RUN),latest) $(if $(HANDLE),--handle $(HANDLE))

set-link:
	uv run python -m app.cli set-link --handle $(HANDLE)

record:
	uv run python -m app.cli record $(if $(WALLETS),--wallets $(WALLETS)) $(if $(SECONDS),--seconds $(SECONDS))

replay:
	uv run python -m app.cli replay --file $(FILE)
