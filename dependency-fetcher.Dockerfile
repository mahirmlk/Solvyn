# Pinned uv is used only to export the lock; runtime package traffic is proxied.
FROM python:3.12-slim AS uv-build
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl     && rm -rf /var/lib/apt/lists/*
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) archive="uv-x86_64-unknown-linux-gnu.tar.gz"; checksum="c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83"; folder="uv-x86_64-unknown-linux-gnu" ;; \
      arm64) archive="uv-aarch64-unknown-linux-gnu.tar.gz"; checksum="71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc"; folder="uv-aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported dependency fetcher architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
      --output /tmp/uv.tar.gz "https://github.com/astral-sh/uv/releases/download/0.13.7/$archive"; \
    echo "$checksum  /tmp/uv.tar.gz" | sha256sum --check --status; \
    mkdir -p /tmp/uv; tar -xzf /tmp/uv.tar.gz -C /tmp/uv; \
    install -m 0555 "/tmp/uv/$folder/uv" /usr/local/bin/uv; \
    rm -rf /tmp/uv /tmp/uv.tar.gz

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY --from=uv-build /usr/local/bin/uv /usr/local/bin/uv
RUN printf '%s\n' 'pip==26.3 --hash=sha256:f3b60b20ffaadd4415ed7957212a38ac63fa4c5fa375bd0f4a797777940fad13' > /tmp/pip-requirements.txt \
    && python -m pip install --no-cache-dir --no-deps --require-hashes -r /tmp/pip-requirements.txt \
    && rm /tmp/pip-requirements.txt \
    && uv --version | grep -Eq '^uv 0\.13\.7([[:space:]]|$)' \
    && python -m pip --version | grep -F "pip 26.3 "
RUN groupadd --system --gid 10002 dependency-fetcher \
    && useradd --system --uid 10002 --gid dependency-fetcher --no-create-home --shell /usr/sbin/nologin dependency-fetcher
WORKDIR /app
COPY --chown=10002:10002 solvyn/__init__.py ./solvyn/__init__.py
COPY --chown=10002:10002 solvyn/sandbox/dependencies.py solvyn/sandbox/fetch_service.py ./solvyn/sandbox/
USER 10002:10002
EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/health', timeout=3)"
CMD ["python", "-m", "solvyn.sandbox.fetch_service"]
