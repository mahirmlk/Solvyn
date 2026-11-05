FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# This is a trusted control service. It alone receives the host Docker socket;
# user commands execute in child containers that never receive that socket.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY solvyn/ ./solvyn/
RUN pip install --no-cache-dir . && pip cache purge \
    && mkdir -p /app/.solvyn/workspaces

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health', timeout=3)"

CMD ["python", "-m", "solvyn.sandbox.service"]
