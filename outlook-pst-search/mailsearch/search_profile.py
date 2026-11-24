"""Timings and short-lived access checks for local search requests."""
from collections import OrderedDict
from contextlib import contextmanager
from secrets import token_urlsafe
from threading import Lock
from time import perf_counter
from uuid import uuid4


STAGES = ("fts", "embed", "vector", "rerank", "access", "context", "generation", "validation")


class SearchProfile:
    def __init__(self, mode):
        self.id = uuid4().hex[:12]
        self.mode = mode
        self.ms = dict.fromkeys(STAGES, 0.0)
        self.lock = Lock()
        self.started = perf_counter()

    @contextmanager
    def measure(self, stage):
        start = perf_counter()
        try:
            yield
        finally:
            with self.lock:
                self.ms[stage] += (perf_counter() - start) * 1000

    def log(self, logger, phase):
        with self.lock:
            logger.info(
                "Search profile id=%s phase=%s mode=%s total_ms=%.2f %s",
                self.id, phase, self.mode, (perf_counter() - self.started) * 1000,
                " ".join(f"{stage}_ms={self.ms[stage]:.2f}" for stage in STAGES),
            )


class SearchTickets:
    """Hold unverified candidates on the server until the UI requests verification."""
    def __init__(self):
        self.items = OrderedDict()
        self.lock = Lock()

    def issue(self, value):
        key = token_urlsafe(24)
        now = perf_counter()
        with self.lock:
            self.expire(now)
            self.items[key] = (now + 120, value)
            if len(self.items) > 32:
                self.items.popitem(last=False)
        return key

    def take(self, key):
        with self.lock:
            self.expire(perf_counter())
            item = self.items.pop(key, None)
        return item[1] if item else None

    def expire(self, now):
        while self.items and next(iter(self.items.values()))[0] <= now:
            self.items.popitem(last=False)
