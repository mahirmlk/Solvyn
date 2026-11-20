FROM python:3.12-slim AS gitleaks-build

ARG TARGETARCH

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) archive="gitleaks_8.30.0_linux_x64.tar.gz"; sha256="79a3ab579b53f71efd634f3aaf7e04a0fa0cf206b7ed434638d1547a2470a66e" ;; \
      arm64) archive="gitleaks_8.30.0_linux_arm64.tar.gz"; sha256="b4cbbb6ddf7d1b2a603088cd03a4e3f7ce48ee7fd449b51f7de6ee2906f5fa2f" ;; \
      *) echo "Unsupported Gitleaks target architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl --fail --location --retry 3 --output /tmp/gitleaks.tar.gz "https://github.com/gitleaks/gitleaks/releases/download/v8.30.0/$archive"; \
    echo "$sha256  /tmp/gitleaks.tar.gz" | sha256sum --check --status; \
    tar --extract --gzip --file /tmp/gitleaks.tar.gz --directory /usr/local/bin gitleaks; \
    rm /tmp/gitleaks.tar.gz

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==9.0.3 ruff==0.15.16 mypy==2.1.0
RUN mkdir -p /opt/solvyn
COPY --from=gitleaks-build /usr/local/bin/gitleaks /usr/local/bin/gitleaks
COPY scripts/gitleaks.toml /opt/solvyn/gitleaks.toml
COPY scripts/gitleaks.ignore /opt/solvyn/gitleaks.ignore
COPY scripts/verify_markdown.py /opt/solvyn/verify_markdown.py
COPY scripts/scan_secrets.py /opt/solvyn/scan_secrets.py
RUN chmod 0444 /opt/solvyn/verify_markdown.py /opt/solvyn/scan_secrets.py
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
WORKDIR /workspace
USER 10001:10001

# Runtime jobs use only this prebuilt image. Package installation and image
# changes happen during trusted image builds, never from an execution request.
CMD ["sleep", "infinity"]
