.PHONY: dev install lint typecheck test test-cov docker-build docker-up docker-down clean pre-commit deploy

# ─── Development ────────────────────────────────────────────
dev:
	uvicorn solvyn.main:app --reload --host 0.0.0.0 --port 8000

dev-frontend:
	cd frontend && npm run dev

install:
	pip install -e ".[dev]"
	cd frontend && npm install

# ─── Quality ────────────────────────────────────────────────
lint:
	ruff check solvyn/ tests/
	ruff format --check solvyn/ tests/

format:
	ruff check --fix solvyn/ tests/
	ruff format solvyn/ tests/

typecheck:
	mypy solvyn/ --follow-imports=skip

test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=solvyn --cov-report=term --cov-report=html

# ─── Docker ─────────────────────────────────────────────────
docker-build:
	docker compose build

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

# ─── Pre-commit ─────────────────────────────────────────────
pre-commit: lint typecheck test

# ─── Kubernetes ─────────────────────────────────────────────
deploy:
	kubectl apply -f k8s/namespace.yaml
	kubectl apply -f k8s/configmap.yaml
	kubectl apply -f k8s/pvc.yaml
	kubectl apply -f k8s/deployment.yaml
	kubectl apply -f k8s/service.yaml
	kubectl apply -f k8s/hpa.yaml
	kubectl apply -f k8s/ingress.yaml

deploy-secrets:
	kubectl apply -f k8s/secrets.yaml

# ─── Cleanup ────────────────────────────────────────────────
clean:
	rm -rf __pycache__ .pytest_cache .mypy_cache htmlcov
	rm -rf frontend/.next frontend/out
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
