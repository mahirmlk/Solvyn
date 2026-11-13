FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pytest==9.0.3 ruff==0.15.16 mypy==2.1.0
RUN mkdir -p /opt/solvyn
COPY scripts/verify_markdown.py /opt/solvyn/verify_markdown.py
RUN chmod 0444 /opt/solvyn/verify_markdown.py
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
WORKDIR /workspace
USER 10001:10001

# Runtime jobs use only this prebuilt image. Package installation and image
# changes happen during trusted image builds, never from an execution request.
CMD ["sleep", "infinity"]
