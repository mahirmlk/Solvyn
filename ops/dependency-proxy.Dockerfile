FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends squid ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /var/spool/squid/*
COPY ops/dependency-proxy.conf /etc/squid/squid.conf
USER proxy
EXPOSE 3128
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD squidclient -h 127.0.0.1 -p 3128 mgr:info >/dev/null 2>&1
CMD ["squid", "-N", "-f", "/etc/squid/squid.conf"]
