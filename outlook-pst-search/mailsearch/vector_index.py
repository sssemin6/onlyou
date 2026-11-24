"""Optional in-memory USearch HNSW side index. SQLite remains authoritative."""
import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from time import monotonic

logger = logging.getLogger("mailsearch.runtime")


class USearchIndex:
    def __init__(self, store):
        import numpy
        from usearch.index import Index

        self.store = store
        self.numpy = numpy
        self.Index = Index
        self.lock = Lock()
        self.indices = OrderedDict()
        self.building = None
        self.failed = {}
        self.stop = Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vector-build")

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.indices.clear()

    def build(self, key):
        model, source, version, dimensions = key
        try:
            index = self.Index(ndim=dimensions, metric="cos", dtype="i8",
                               connectivity=16, expansion_add=96, expansion_search=96)
            with self.store.connection() as conn:
                rows = conn.execute(
                    """SELECT v.passage,v.value FROM mails m JOIN passages p ON p.mail=m.id
                       JOIN searchable_vectors v ON v.passage=p.id
                       WHERE m.source=? AND m.version=? AND v.model=? AND length(v.value)=?""",
                    (source, version, model, dimensions * 4),
                )
                while not self.stop.is_set():
                    batch = rows.fetchmany(1024)
                    if not batch:
                        break
                    keys = self.numpy.asarray([r["passage"] for r in batch], dtype="uint64")
                    values = self.numpy.frombuffer(b"".join(r["value"] for r in batch), dtype="<f4")
                    index.add(keys, self.numpy.ascontiguousarray(values.reshape(-1, dimensions), dtype="float32"),
                              threads=1)
                rows.close()
            with self.lock:
                if not self.stop.is_set():
                    self.indices[key] = index if len(index) else None
                    self.indices.move_to_end(key)
                    while len(self.indices) > 8:
                        self.indices.popitem(last=False)
            logger.info("Vector shard ready (source=%d, version=%d, vectors=%d)", source, version, len(index))
        except (OSError, RuntimeError, ValueError, TypeError, MemoryError) as exc:
            with self.lock:
                self.failed[key] = monotonic() + 60
            logger.warning("USearch build unavailable (source=%d, error=%s)", source, type(exc).__name__)
        finally:
            with self.lock:
                self.building = None

    def candidates(self, conn, vector, model, filters, limit):
        wanted = []
        for row in conn.execute("SELECT id,active,pending FROM sources WHERE active IS NOT NULL OR pending IS NOT NULL"):
            if filters.get("source") and filters["source"] != row["id"]:
                continue
            if row["active"] is None:
                # A first import is mutable until its version is published.
                return None
            wanted.append((model, row["id"], row["active"], len(vector)))
        if len(wanted) > 8:
            return None
        with self.lock:
            self.failed = {key: until for key, until in self.failed.items() if until > monotonic()}
            missing = next((key for key in wanted if key not in self.indices), None)
            if missing:
                if self.building is None and missing not in self.failed and not self.stop.is_set():
                    self.building = missing
                    self.pool.submit(self.build, missing)
                return None
            indices = [self.indices[key] for key in wanted if self.indices[key] is not None]
            for key in wanted:
                self.indices.move_to_end(key)
            # A replaced active version can never be selected again.
            for key in tuple(self.indices):
                if any(key[:2] == active[:2] and key[2] != active[2] for active in wanted):
                    self.indices.pop(key)
        query = self.numpy.asarray(vector, dtype="float32")
        where, params = self.store.scope(filters)
        count = max(limit * 4, 128)
        try:
            while indices:
                scored = []
                for index in indices:
                    matches = index.search(query, min(count, len(index)), threads=1)
                    scored.extend((float(match.distance), int(match.key)) for match in matches)
                scored.sort()
                keys = [key for _, key in scored]
                allowed = {r[0] for r in conn.execute(
                    f"""SELECT p.id FROM passages p JOIN mails m ON m.id=p.mail JOIN sources s ON s.id=m.source
                        WHERE p.id IN (SELECT value FROM json_each(?)) AND {where}""",
                    (json.dumps(keys), *params),
                )}
                chosen = [key for key in keys if key in allowed]
                if len(chosen) >= limit or all(len(index) <= count for index in indices):
                    return chosen[:2400]
                if count >= 2400:
                    return None
                count = min(2400, count * 4)
        except (RuntimeError, ValueError, TypeError, MemoryError) as exc:
            logger.warning("USearch query unavailable (%s)", type(exc).__name__)
            return None
        return []
