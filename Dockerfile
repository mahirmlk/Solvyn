FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system --gid 10001 solvyn && useradd --system --uid 10001 --gid solvyn --create-home solvyn

COPY pyproject.toml ./
COPY solvyn/ ./solvyn/
COPY alembic.ini ./alembic.ini
RUN mkdir -p /app/.solvyn/workspaces && chown -R solvyn:solvyn /app/.solvyn
RUN pip install --no-cache-dir . && pip cache purge

RUN chown -R solvyn:solvyn /app

USER solvyn
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

CMD ["uvicorn", "solvyn.main:app", "--host", "0.0.0.0", "--port", "8000"]
