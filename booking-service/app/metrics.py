"""Tiny Prometheus-style metrics (no extra packages). Scraped from /metrics."""
import threading
import time

START = time.time()
_lock = threading.Lock()
_counts = {}   # (method, path, status) -> number of requests
_timing = {}   # (method, path) -> [count, total seconds]


def record(method: str, path: str, status: int, seconds: float) -> None:
    with _lock:
        _counts[(method, path, str(status))] = _counts.get((method, path, str(status)), 0) + 1
        entry = _timing.setdefault((method, path), [0, 0.0])
        entry[0] += 1
        entry[1] += seconds


def render(gauges: dict) -> str:
    """gauges: {metric_name: (help text, value)}"""
    lines = ["# HELP fest_http_requests_total HTTP requests handled", "# TYPE fest_http_requests_total counter"]
    with _lock:
        for (method, path, status), n in sorted(_counts.items()):
            lines.append(f'fest_http_requests_total{{method="{method}",path="{path}",status="{status}"}} {n}')
        lines += ["# HELP fest_http_request_duration_seconds Time spent handling requests",
                  "# TYPE fest_http_request_duration_seconds summary"]
        for (method, path), (n, total) in sorted(_timing.items()):
            label = f'method="{method}",path="{path}"'
            lines.append(f"fest_http_request_duration_seconds_count{{{label}}} {n}")
            lines.append(f"fest_http_request_duration_seconds_sum{{{label}}} {total:.6f}")
    for name, (help_text, value) in gauges.items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}"]
    lines += ["# HELP fest_uptime_seconds Seconds since the booking service started",
              "# TYPE fest_uptime_seconds gauge", f"fest_uptime_seconds {time.time() - START:.0f}"]
    return "\n".join(lines) + "\n"
