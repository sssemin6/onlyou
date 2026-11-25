"""Optional disk-backed USearch HNSW side index. SQLite remains authoritative."""
import hashlib
import json
import logging
import os
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from tempfile import NamedTemporaryFile
from threading import Event, Lock
from time import monotonic

logger = logging.getLogger("mailsearch.runtime")
SHARD_FILE = re.compile(r"v1-([0-9]+)-([0-9]+)-[0-9]+-[0-9a-f]{64}\.usearch")
ACTIVE_SHARDS = "SELECT id,active FROM sources WHERE active IS NOT NULL AND state<>'removing'"


def prune_vector_files(store, active=None):
    root = store.root / "vectors"
    if not root.is_dir():
        return
    try:
        if active is None:
            with store.connection() as conn:
                active = dict(conn.execute(ACTIVE_SHARDS))
        for path in root.glob("*.usearch"):
            match = SHARD_FILE.fullmatch(path.name)
            if match and active.get(int(match[1])) != int(match[2]):
                path.unlink(missing_ok=True)
    except (OSError, sqlite3.Error) as exc:
        logger.warning("Vector shard cleanup deferred (%s)", type(exc).__name__)


class USearchIndex:
    def __init__(self, store):
        import numpy
        from usearch.index import Index

        self.store = store
        self.root = store.root / "vectors"
        self.numpy = numpy
        self.Index = Index
        self.lock = Lock()
        self.indices = {}
        self.building = set()
        self.failed = {}
        self.stop = Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vector-build")
        self.prune()
        if self.root.is_dir():
            for path in self.root.glob(".shard-*.tmp"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Temporary vector shard cleanup deferred")

    def close(self):
        self.stop.set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.indices.clear()
        prune_vector_files(self.store)

    def prune(self):
        with self.lock:
            with self.store.connection() as conn:
                active = dict(conn.execute(ACTIVE_SHARDS))
            self.indices = {key: index for key, index in self.indices.items() if active.get(key[1]) == key[2]}
            prune_vector_files(self.store, active)

    def path(self, key):
        model, source, version, dimensions = key
        digest = hashlib.sha256(model.encode()).hexdigest()
        return self.root / f"v1-{source}-{version}-{dimensions}-{digest}.usearch"

    def new_index(self, dimensions):
        return self.Index(ndim=dimensions, metric="cos", dtype="i8",
                          connectivity=16, expansion_add=96, expansion_search=96)

    def build(self, key):
        model, source, version, dimensions = key
        temporary = None
        try:
            if self.stop.is_set():
                return
            index = self.new_index(dimensions)
            with self.store.connection() as conn:
                rows = conn.execute(
                    """SELECT v.passage,v.value FROM mails m CROSS JOIN passages p ON p.mail=m.id
                       CROSS JOIN vectors v ON v.model=? AND v.passage=p.id
                       WHERE m.source=? AND m.version=? AND length(v.value)=?
                       UNION ALL
                       SELECT r.passage,v.value FROM mails m CROSS JOIN passages p ON p.mail=m.id
                       CROSS JOIN embedding_refs r ON r.model=? AND r.passage=p.id
                       CROSS JOIN embedding_values v ON v.model=r.model AND v.digest=r.digest
                       WHERE m.source=? AND m.version=? AND length(v.value)=?""",
                    (model, source, version, dimensions * 4) * 2,
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
            if self.stop.is_set():
                return
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=self.root, prefix=".shard-", suffix=".tmp", delete=False) as stream:
                temporary = stream.name
            index.save(temporary)
            with self.lock, self.store.connection() as conn:
                if self.stop.is_set() or not conn.execute(
                    ACTIVE_SHARDS + " AND id=? AND active=?", (source, version),
                ).fetchone():
                    return
                path = self.path(key)
                os.replace(temporary, path)
                temporary = None
                view = self.new_index(dimensions)
                view.view(str(path))
                self.indices[key] = view
            logger.info("Vector shard ready (source=%d, version=%d, vectors=%d)", source, version, len(index))
        except (OSError, RuntimeError, ValueError, TypeError, MemoryError, sqlite3.Error) as exc:
            with self.lock:
                self.failed[key] = monotonic() + 60
            logger.warning("USearch build unavailable (source=%d, error=%s)", source, type(exc).__name__)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    logger.warning("Temporary vector shard cleanup deferred")
            with self.lock:
                self.building.discard(key)

    def candidates(self, conn, vector, model, filters, limit):
        wanted = []
        allowed_sources = filters.get("vector_sources")
        for row in conn.execute("""SELECT id,active FROM sources WHERE state<>'removing'
                                  AND (active IS NOT NULL OR pending IS NOT NULL)"""):
            if ((filters.get("source") and filters["source"] != row["id"])
                    or (allowed_sources is not None and row["id"] not in allowed_sources)):
                continue
            if row["active"] is None:
                # A first import is mutable until its version is published.
                return None
            wanted.append((model, row["id"], row["active"], len(vector)))
        with self.lock:
            if self.stop.is_set():
                return None
            self.failed = {key: until for key, until in self.failed.items() if until > monotonic()}
            for key in wanted:
                if key in self.indices or key in self.building or key in self.failed:
                    continue
                path = self.path(key)
                try:
                    if path.is_file():
                        view = self.new_index(key[3])
                        view.view(str(path))
                        if view.ndim != key[3]:
                            raise ValueError("vector dimensions")
                        self.indices[key] = view
                        logger.info("Vector shard mapped (source=%d, version=%d)", key[1], key[2])
                        continue
                except (OSError, RuntimeError, ValueError, TypeError, MemoryError) as exc:
                    logger.warning("Vector shard could not be mapped (source=%d, error=%s)", key[1], type(exc).__name__)
                self.building.add(key)
                self.pool.submit(self.build, key)
            if any(key not in self.indices for key in wanted):
                return None
            indices = [self.indices[key] for key in wanted if len(self.indices[key])]
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
                    f"""SELECT p.id FROM json_each(?) ids CROSS JOIN passages p ON p.id=ids.value
                        CROSS JOIN mails m ON m.id=p.mail CROSS JOIN sources s ON s.id=m.source WHERE {where}""",
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
