UV := env -u VIRTUAL_ENV uv
FLOW ?= process_review
K ?= 1
CASE ?=

.PHONY: test lint check eval run graph compliance serve web

test:       ## offline unit tests, no API calls
	$(UV) run pytest -q

lint:
	$(UV) run ruff check . && $(UV) run ruff format --check .

check: lint test

eval:       ## live API: run a flow's eval set (make eval FLOW=process_qa K=3)
	$(UV) run python -m flows.$(FLOW).evals --k $(K)

run:        ## live API: one eval case through a flow (make run FLOW=process_review CASE=<id>)
	$(UV) run python -m kit.cli run $(FLOW) $(if $(CASE),--case $(CASE))

graph:      ## offline: redraw flows/$(FLOW)/GRAPH.md from the compiled graph
	$(UV) run python -m kit.cli graph $(FLOW)

compliance: ## offline: audit-control check for a flow (make compliance FLOW=process_qa)
	$(UV) run python -m flows.$(FLOW).compliance

serve:      ## the API on :8000
	$(UV) run uvicorn app.api:app --port 8000

web:        ## the React front end on :5173 (needs `make serve` running)
	cd web && npm run dev
