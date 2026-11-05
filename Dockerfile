FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --system solvyn && useradd --system --gid solvyn --create-home solvyn

COPY pyproject.toml ./
COPY solvyn/ ./solvyn/
COPY alembic.ini ./alembic.ini
RUN pip install --no-cache-dir . && pip cache purge

RUN chown -R solvyn:solvyn /app

USER solvyn
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=3)"

CMD ["uvicorn", "solvyn.main:app", "--host", "0.0.0.0", "--port", "8000"]
