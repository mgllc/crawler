#!/usr/bin/env python3
"""Self-healing capabilities for the crawler.

Provides four cooperating components that make the crawler robust and
auto-recovering under adverse network conditions:

* :class:`CircuitBreaker`      – stops hammering domains that keep failing.
* :class:`AdaptiveRetryPolicy` – adjusts retries and backoff to HTTP codes.
* :class:`HealthMonitor`       – tracks per-domain statistics in real time.
* :class:`HealthCheck`         – aggregates a single health snapshot.
"""

from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

class CircuitState(Enum):
    """Operational state of a circuit for one domain."""

    CLOSED = "closed"       # normal – requests go through
    OPEN = "open"           # failing – requests are blocked
    HALF_OPEN = "half_open" # recovering – one probe request allowed


@dataclass
class DomainStats:
    """Accumulated statistics for a single crawled domain."""

    requests: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_failure_time: float | None = None
    last_success_time: float | None = None
    total_bytes: int = 0
    total_elapsed_ms: int = 0
    http_status_counts: dict[int, int] = field(default_factory=dict)


class CircuitBreaker:
    """Per-domain circuit breaker.

    Transitions
    -----------
    CLOSED   → OPEN       once *failure_threshold* consecutive failures occur.
    OPEN     → HALF_OPEN  once *recovery_timeout* seconds have elapsed.
    HALF_OPEN → CLOSED    on the first successful probe.
    HALF_OPEN → OPEN      on a failed probe.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._states: dict[str, CircuitState] = {}
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self._opened_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_open(self, domain: str) -> bool:
        """Return ``True`` when the circuit is open and requests should be skipped."""
        with self._lock:
            state = self._states.get(domain, CircuitState.CLOSED)
            if state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at.get(domain, 0.0)
                if elapsed >= self.recovery_timeout:
                    self._states[domain] = CircuitState.HALF_OPEN
                    return False
                return True
            return False

    def record_success(self, domain: str) -> None:
        """Close the circuit after a successful request."""
        with self._lock:
            self._consecutive_failures[domain] = 0
            self._states[domain] = CircuitState.CLOSED

    def record_failure(self, domain: str) -> None:
        """Record a failure; open the circuit when the threshold is reached."""
        with self._lock:
            self._consecutive_failures[domain] += 1
            if self._consecutive_failures[domain] >= self.failure_threshold:
                if self._states.get(domain) != CircuitState.OPEN:
                    self._opened_at[domain] = time.monotonic()
                self._states[domain] = CircuitState.OPEN

    def get_state(self, domain: str) -> CircuitState:
        """Return the current :class:`CircuitState` for *domain*."""
        with self._lock:
            return self._states.get(domain, CircuitState.CLOSED)


# ---------------------------------------------------------------------------
# Adaptive Retry Policy
# ---------------------------------------------------------------------------

class AdaptiveRetryPolicy:
    """Retry policy that adapts backoff to HTTP status codes.

    Retryable responses
    -------------------
    * ``429`` Too Many Requests  – uses *rate_limit_backoff* as the base delay.
    * ``500 / 502 / 503 / 504``  – uses *base_backoff* with exponential growth.
    * Network / connection errors (no status code) – retries with base backoff.

    Non-retryable responses
    -----------------------
    All other 4xx client errors are not retried.
    """

    RETRYABLE_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_backoff: float = 60.0,
        rate_limit_backoff: float = 5.0,
        jitter: bool = True,
    ) -> None:
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.rate_limit_backoff = rate_limit_backoff
        self.jitter = jitter

    def should_retry(
        self,
        attempt: int,
        status_code: int | None,
        error: Exception | None,
    ) -> bool:
        """Return ``True`` if the request should be retried."""
        if attempt > self.max_retries:
            return False
        if status_code is not None:
            return status_code in self.RETRYABLE_STATUSES
        # Network / connection error – always retry until max_retries.
        return error is not None

    def get_backoff(self, attempt: int, status_code: int | None) -> float:
        """Return the number of seconds to wait before the *attempt*-th retry."""
        base = self.rate_limit_backoff if status_code == 429 else self.base_backoff
        delay = min(base * (2 ** (attempt - 1)), self.max_backoff)
        if self.jitter:
            delay *= 0.5 + random.random() * 0.5
        return delay


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------

class HealthMonitor:
    """Real-time monitor for crawler operations.

    Tracks per-domain request/failure statistics and raises alerts when
    anomalous patterns are detected (high failure rate, consecutive domain
    failures).
    """

    def __init__(
        self,
        failure_rate_threshold: float = 0.5,
        window_size: int = 20,
    ) -> None:
        self.failure_rate_threshold = failure_rate_threshold
        self.window_size = window_size
        self._domain_stats: dict[str, DomainStats] = defaultdict(DomainStats)
        self._recent_outcomes: deque[bool] = deque(maxlen=window_size)
        self._alerts: list[str] = []
        self._lock = threading.Lock()
        self._start_time = time.monotonic()

    def record_request(
        self,
        domain: str,
        success: bool,
        status: int | None = None,
        elapsed_ms: int | None = None,
        bytes_read: int | None = None,
    ) -> None:
        """Record the outcome of a single HTTP request."""
        with self._lock:
            stats = self._domain_stats[domain]
            stats.requests += 1
            if success:
                stats.consecutive_failures = 0
                stats.last_success_time = time.monotonic()
                if elapsed_ms is not None:
                    stats.total_elapsed_ms += elapsed_ms
                if bytes_read is not None:
                    stats.total_bytes += bytes_read
            else:
                stats.failures += 1
                stats.consecutive_failures += 1
                stats.last_failure_time = time.monotonic()
            if status is not None:
                stats.http_status_counts[status] = (
                    stats.http_status_counts.get(status, 0) + 1
                )
            self._recent_outcomes.append(success)
            self._detect_anomalies(domain, stats)

    def _detect_anomalies(self, domain: str, stats: DomainStats) -> None:
        """Generate alerts for detected anomalies (must be called with lock held)."""
        if len(self._recent_outcomes) >= self.window_size:
            failures = sum(1 for ok in self._recent_outcomes if not ok)
            rate = failures / len(self._recent_outcomes)
            if rate >= self.failure_rate_threshold:
                msg = (
                    f"High failure rate {rate:.0%} in the last "
                    f"{self.window_size} requests"
                )
                if msg not in self._alerts:
                    self._alerts.append(msg)
        if stats.consecutive_failures >= 3:
            msg = (
                f"Domain {domain} has {stats.consecutive_failures} "
                "consecutive failures"
            )
            if msg not in self._alerts:
                self._alerts.append(msg)

    def get_health_status(self) -> dict:
        """Return a snapshot dictionary describing overall crawler health."""
        with self._lock:
            total_req = sum(s.requests for s in self._domain_stats.values())
            total_fail = sum(s.failures for s in self._domain_stats.values())
            failure_rate = total_fail / total_req if total_req > 0 else 0.0
            return {
                "healthy": (
                    failure_rate < self.failure_rate_threshold
                    and not self._alerts
                ),
                "uptime_seconds": round(time.monotonic() - self._start_time, 2),
                "total_requests": total_req,
                "total_failures": total_fail,
                "failure_rate": round(failure_rate, 4),
                "domains_crawled": len(self._domain_stats),
                "alerts": list(self._alerts),
                "domain_stats": {
                    domain: {
                        "requests": s.requests,
                        "failures": s.failures,
                        "consecutive_failures": s.consecutive_failures,
                    }
                    for domain, s in self._domain_stats.items()
                },
            }

    def clear_alerts(self) -> None:
        """Clear all active alerts."""
        with self._lock:
            self._alerts.clear()


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

class HealthCheck:
    """Aggregated health-check workflow for the crawler and its components.

    Combines :class:`HealthMonitor` and :class:`CircuitBreaker` data into a
    single status report that can be polled or logged periodically.
    """

    def __init__(
        self,
        monitor: HealthMonitor,
        circuit_breaker: CircuitBreaker,
    ) -> None:
        self.monitor = monitor
        self.circuit_breaker = circuit_breaker

    def run(self) -> dict:
        """Run a full health check and return a status dictionary."""
        health = self.monitor.get_health_status()
        return {
            "status": "healthy" if health["healthy"] else "degraded",
            "details": health,
        }
