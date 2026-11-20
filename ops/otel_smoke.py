from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

TEMPO = "http://127.0.0.1:3200"
COLLECTOR = "http://127.0.0.1:4318"


def get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def wait_for_ready(url: str, label: str, timeout: float = 90) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            status, body = get(url)
            if status == 200:
                print(f"{label} ready")
                return
            last_error = f"HTTP {status}: {body[:200]}"
        except (OSError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"{label} did not become ready: {last_error}")


def main() -> None:
    wait_for_ready(f"{TEMPO}/ready", "Tempo")
    wait_for_ready("http://127.0.0.1:13133/", "OpenTelemetry Collector")

    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    end_time = time.time_ns()
    start_time = end_time - 1_000_000
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "solvyn-tracing-smoke"}}
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "solvyn-ci-smoke"},
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "solvyn-otel-smoke",
                                "kind": 1,
                                "startTimeUnixNano": str(start_time),
                                "endTimeUnixNano": str(end_time),
                            }
                        ],
                    }
                ],
            }
        ]
    }
    request = urllib.request.Request(
        f"{COLLECTOR}/v1/traces",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        if response.status != 200:
            raise SystemExit(f"Collector rejected OTLP span: HTTP {response.status}")
    print(f"Collector accepted trace {trace_id}")

    deadline = time.monotonic() + 90
    trace_url = f"{TEMPO}/api/v2/traces/{trace_id}"
    last_error = "trace not visible yet"
    while time.monotonic() < deadline:
        try:
            status, body = get(trace_url)
            if status == 200 and "solvyn-otel-smoke" in body:
                print("Tempo returned the ingested smoke span")
                return
            last_error = f"HTTP {status}: {body[:300]}"
        except (OSError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise SystemExit(f"Tempo did not return the smoke span: {last_error}")


if __name__ == "__main__":
    main()
