"""The docs read cache: identity, racy window, eviction, bounds and copies.

No Neo4j. Every read through the cache must equal `docs.read_front_matter`
for the file's current bytes; these tests try to make it differ.
"""

import os
import random
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgraph.config.yaml_bound import YAML_MAX_NODES
from devgraph.indexer.providers import docs, docs_cache
from devgraph.paths import MAX_CONFIG_BYTES

NO_CTIME = pytest.mark.skipif(sys.platform == "win32", reason="no change time; see spec Known limits")
SECOND = 1_000_000_000


@pytest.fixture(autouse=True)
def _forget_roots():
    yield
    with docs_cache._lock:
        roots = {root for root, _ in docs_cache._store}
    for root in roots:
        docs_cache.forget(Path(root))


@pytest.fixture
def aged(monkeypatch):
    """Every file looks at least 10 s old, so successful parses are stored."""
    monkeypatch.setattr(docs_cache, "_clock", lambda: time.time_ns() + 10 * SECOND)


@pytest.fixture
def spy(monkeypatch):
    """Counts real parses through `docs.read_front_matter`."""
    calls = []
    real = docs.read_front_matter

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(docs, "read_front_matter", counting)
    return calls


def wait_ctime_advance(path, before_ns=None, timeout=5.0):
    """Touch a probe beside `path` until its ctime passes `before_ns` (default: `path`'s
    own ctime), so the next write to `path` gets a later ctime. Fails after `timeout`
    seconds. On Windows st_ctime is the creation time, so the mtime is waited on instead."""
    field = "st_mtime_ns" if sys.platform == "win32" else "st_ctime_ns"
    if before_ns is None:
        before_ns = getattr(os.stat(path), field)
    probe = Path(path).parent / ".ctime-probe"
    deadline = time.monotonic() + timeout
    try:
        while True:
            probe.write_bytes(b"x")
            if getattr(os.stat(probe), field) > before_ns:
                return
            if time.monotonic() > deadline:
                pytest.fail(f"{field} of {probe} did not pass {before_ns} within {timeout}s")
            time.sleep(0.001)
    finally:
        probe.unlink(missing_ok=True)


def test_wait_ctime_advance_times_out(tmp_path, monkeypatch):
    p = tmp_path / "a.md"
    write(p, "x")
    real = os.stat
    frozen = real(p)
    monkeypatch.setattr(os, "stat", lambda *a, **k: frozen)
    start = time.monotonic()
    with pytest.raises(pytest.fail.Exception):
        wait_ctime_advance(p, timeout=0.05)
    assert time.monotonic() - start < 2
    assert ".ctime-probe" not in os.listdir(tmp_path)


def write(path, text):
    Path(path).write_text(text, encoding="utf-8")


def doc(id_, extra=""):
    return f"---\nid: {id_}\ntags: [a]\n{extra}---\nbody\n"


def delta(before):
    after = docs_cache.stats()
    return {k: after[k] - before[k] for k in ("hits", "misses", "fresh")}


def charge_of(path, rel=""):
    value = docs.read_front_matter(path)[0]
    return docs_cache.ENTRY_OVERHEAD + sys.getsizeof(rel) + docs_cache._deep_size(value)


def live_charges():
    """The sum of every live entry's charge, recomputed from the stored values."""
    with docs_cache._lock:
        items = list(docs_cache._store.items())
    for (_, rel), e in items:
        assert e.charge == docs_cache.ENTRY_OVERHEAD + sys.getsizeof(rel) + docs_cache._deep_size(e.value)
    return sum(e.charge for _, e in items)


def test_hit_and_miss(tmp_path, aged, spy):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    before = docs_cache.stats()
    first = docs_cache.read(tmp_path, "a.md", p)
    assert delta(before) == {"hits": 0, "misses": 1, "fresh": 0}
    second = docs_cache.read(tmp_path, "a.md", p)
    assert delta(before) == {"hits": 1, "misses": 1, "fresh": 0}
    assert first == second == ({"id": "ADR-0001", "tags": ["a"]}, None)
    assert len(spy) == 1
    assert docs_cache.stats()["entries"] == 1


@NO_CTIME
def test_same_size_mtime_restored(tmp_path, aged):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0001"
    old = os.stat(p)
    write(p, doc("ADR-0002"))
    wait_ctime_advance(p, old.st_ctime_ns)
    os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert os.stat(p).st_size == old.st_size and os.stat(p).st_mtime_ns == old.st_mtime_ns
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0002"


def test_same_size_real_clock(tmp_path):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0001"
    old = os.stat(p)
    write(p, doc("ADR-0002"))
    os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0002"
    assert docs_cache.stats()["entries"] == 0


def fake_stat(*, dev=1, ino=2, size=3, mtime=0, ctime=0):
    return SimpleNamespace(st_dev=dev, st_ino=ino, st_size=size, st_mtime_ns=mtime, st_ctime_ns=ctime)


def test_racy_rule():
    now = 100 * SECOND
    assert not docs_cache._trusted(fake_stat(mtime=now - 2_999_000_000, ctime=now - 2_999_000_000), now)
    assert docs_cache._trusted(fake_stat(mtime=now - 3 * SECOND, ctime=now - 3 * SECOND), now)
    assert not docs_cache._trusted(fake_stat(mtime=now + SECOND, ctime=now - 10 * SECOND), now)
    assert not docs_cache._trusted(fake_stat(mtime=now - 10 * SECOND, ctime=now - SECOND), now)
    assert docs_cache.RACY_WINDOW_NS == 3 * SECOND


def test_identity_has_ctime_and_inode():
    base = fake_stat(mtime=5, ctime=6)
    assert docs_cache._identity(base) == docs_cache._identity(fake_stat(mtime=5, ctime=6))
    assert docs_cache._identity(base) != docs_cache._identity(fake_stat(mtime=5, ctime=7))
    assert docs_cache._identity(base) != docs_cache._identity(fake_stat(mtime=5, ctime=6, ino=9))


def test_atomic_save(tmp_path, aged):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0001"
    old = os.stat(p)
    tmp = tmp_path / "a.md.tmp"
    write(tmp, doc("ADR-0002"))
    os.utime(tmp, ns=(old.st_atime_ns, old.st_mtime_ns))
    os.replace(tmp, p)
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0002"


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_retarget(tmp_path, aged):
    one, two = tmp_path / "one.md", tmp_path / "two.md"
    write(one, doc("ADR-0001"))
    write(two, doc("ADR-0002"))
    st = os.stat(one)
    os.utime(two, ns=(st.st_atime_ns, st.st_mtime_ns))
    link = tmp_path / "link.md"
    link.symlink_to(one)
    assert docs_cache.read(tmp_path, "link.md", link)[0]["id"] == "ADR-0001"
    link.unlink()
    link.symlink_to(two)
    assert docs_cache.read(tmp_path, "link.md", link)[0]["id"] == "ADR-0002"


def test_changed_during_read(tmp_path, aged, monkeypatch):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    real = docs.read_front_matter
    calls = []

    def rewriting(path):
        calls.append(path)
        result = real(path)
        if len(calls) == 1:
            write(p, doc("ADR-0002", "extra: yes\n"))
        return result

    monkeypatch.setattr(docs, "read_front_matter", rewriting)
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0001"
    assert docs_cache.stats()["entries"] == 0
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0002"
    assert len(calls) == 2


def test_stat_failure(tmp_path, aged, monkeypatch):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    docs_cache.read(tmp_path, "a.md", p)
    assert docs_cache.stats()["entries"] == 1

    def failing(path):
        raise OSError("stat failed")

    monkeypatch.setattr(docs_cache, "_stat", failing)
    assert docs_cache.read(tmp_path, "a.md", p) == docs.read_front_matter(p)
    assert docs_cache.stats()["entries"] == 0
    assert docs_cache.read_fresh(tmp_path, "a.md", p) == docs.read_front_matter(p)
    assert docs_cache.stats()["entries"] == 0


@NO_CTIME
@pytest.mark.skipif(sys.platform != "win32" and os.geteuid() == 0, reason="root reads a mode-000 file")
def test_read_fresh_evicts(tmp_path, aged, monkeypatch, spy):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    cached = os.stat(p)
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0001"
    write(p, doc("ADR-0002"))
    os.chmod(p, 0)
    assert docs_cache.read_fresh(tmp_path, "a.md", p) == (None, "could not be read")
    os.chmod(p, 0o644)
    os.utime(p, ns=(cached.st_atime_ns, cached.st_mtime_ns))
    monkeypatch.setattr(docs_cache, "_stat", lambda path: cached)
    spy.clear()
    assert docs_cache.read(tmp_path, "a.md", p)[0]["id"] == "ADR-0002"
    assert len(spy) == 1


@pytest.mark.parametrize("case", ["invalid_yaml", "not_mapping", "fifo", "too_large", "missing"])
def test_not_cached(tmp_path, aged, case):
    p = tmp_path / "a.md"
    if case == "invalid_yaml":
        write(p, "---\nid: [unclosed\n---\n")
    elif case == "not_mapping":
        write(p, "---\n- a\n- b\n---\n")
    elif case == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("no FIFOs on this platform")
        os.mkfifo(p)
    elif case == "too_large":
        write(p, "---\nid: x\n---\n" + "a" * MAX_CONFIG_BYTES)
    expected = docs.read_front_matter(p)
    assert expected[0] is None
    for _ in range(2):
        assert docs_cache.read(tmp_path, "a.md", p) == expected
        assert docs_cache.read_fresh(tmp_path, "a.md", p) == expected
    assert docs_cache.stats()["entries"] == 0


def _mutate(values):
    values["added"] = 1
    values["tags"].append("mutated")


def test_copies(tmp_path, aged):
    p = tmp_path / "a.md"
    write(p, doc("ADR-0001"))
    _mutate(docs_cache.read(tmp_path, "a.md", p)[0])
    assert docs_cache.read(tmp_path, "a.md", p) == docs.read_front_matter(p)
    _mutate(docs_cache.read_fresh(tmp_path, "a.md", p)[0])
    assert docs_cache.read(tmp_path, "a.md", p) == docs.read_front_matter(p)
    _mutate(docs_cache.read(tmp_path, "a.md", p)[0])
    assert docs_cache.read(tmp_path, "a.md", p) == docs.read_front_matter(p)


def test_copies_keep_aliases(tmp_path, aged):
    p = tmp_path / "a.md"
    write(p, "---\na: &x [1]\nb: *x\n---\n")
    fresh = docs.read_front_matter(p)[0]
    assert fresh["a"] is fresh["b"]
    docs_cache.read(tmp_path, "a.md", p)
    hit = docs_cache.read(tmp_path, "a.md", p)[0]
    assert docs_cache.stats()["hits"] >= 1
    assert hit["a"] is hit["b"]


def test_deep_size():
    x = ["s" * 1000]
    shared = docs_cache._deep_size({"a": x, "b": x})
    assert shared < docs_cache._deep_size({"a": x}) + 1000
    assert shared < docs_cache._deep_size({"a": x, "b": ["s" * 1000 + "t"]})
    loop = []
    loop.append(loop)
    assert docs_cache._deep_size(loop) >= sys.getsizeof(loop)
    top = {"a": 1}
    assert docs_cache._deep_size(top) >= sys.getsizeof(top)


def _files(root, n, extra=""):
    paths = []
    for i in range(n):
        p = root / f"f{i}.md"
        write(p, doc(f"ADR-{i:04d}", extra))
        paths.append(p)
    return paths


def _keys(root):
    key = docs_cache._root_key(root)
    with docs_cache._lock:
        return [rel for r, rel in docs_cache._store if r == key]


def test_bounds_entries_lru(tmp_path, aged, monkeypatch):
    monkeypatch.setattr(docs_cache, "MAX_ENTRIES", 2)
    a, b, c = _files(tmp_path, 3)
    docs_cache.read(tmp_path, "a", a)
    docs_cache.read(tmp_path, "b", b)
    docs_cache.read(tmp_path, "a", a)  # a hit: a is now most recent
    docs_cache.read(tmp_path, "c", c)
    assert _keys(tmp_path) == ["a", "c"]
    assert docs_cache.stats()["bytes"] == live_charges()


def test_bounds_bytes(tmp_path, aged, monkeypatch):
    paths = _files(tmp_path, 5)
    one = charge_of(paths[0], "0")
    monkeypatch.setattr(docs_cache, "MAX_BYTES", one * 2 + one // 2)
    for i, p in enumerate(paths):
        docs_cache.read(tmp_path, str(i), p)
    assert _keys(tmp_path) == ["3", "4"]
    assert docs_cache.stats()["bytes"] == live_charges() <= docs_cache.MAX_BYTES


def test_bounds_entry_too_large(tmp_path, aged, monkeypatch):
    (p,) = _files(tmp_path, 1)
    monkeypatch.setattr(docs_cache, "MAX_ENTRY_BYTES", charge_of(p, "a") + 100)
    docs_cache.read(tmp_path, "a", p)
    assert _keys(tmp_path) == ["a"]
    write(p, doc("ADR-0001", "big: " + "x" * 2000 + "\n"))
    assert docs_cache.read(tmp_path, "a", p) == docs.read_front_matter(p)
    assert _keys(tmp_path) == []
    assert docs_cache.stats()["bytes"] == 0


def test_bounds_replace_keeps_bytes(tmp_path, aged):
    a, b = _files(tmp_path, 2)
    docs_cache.read(tmp_path, "a", a)
    docs_cache.read(tmp_path, "b", b)
    write(a, doc("ADR-0009", "more: [1, 2, 3, 4, 5]\n"))
    docs_cache.read(tmp_path, "a", a)
    docs_cache.read_fresh(tmp_path, "b", b)
    assert _keys(tmp_path) == ["a", "b"]
    assert docs_cache.read(tmp_path, "a", a) == docs.read_front_matter(a)
    assert docs_cache.stats()["bytes"] == live_charges()


@pytest.fixture(scope="module")
def hostile_root(tmp_path_factory):
    return tmp_path_factory.mktemp("hostile")


def test_hostile_many_files(hostile_root, aged):
    root = hostile_root / "many"
    root.mkdir()
    n = docs_cache.MAX_ENTRIES + 1000
    for i in range(n):
        (root / f"{i}.md").write_bytes(b"---\na: 1\n---\n")
    for i in range(n):
        docs_cache.read(root, f"{i}.md", root / f"{i}.md")
    assert docs_cache.stats()["entries"] <= docs_cache.MAX_ENTRIES
    assert docs_cache.stats()["bytes"] <= docs_cache.MAX_BYTES


def test_hostile_large_entries(hostile_root, aged, monkeypatch):
    root = hostile_root / "large"
    root.mkdir()
    n_items = YAML_MAX_NODES - 100
    body = "".join(f"  - {i:08d}{'v' * 45}\n" for i in range(n_items))
    text = f"---\nitems:\n{body}---\n"
    first = root / "0.md"
    write(first, text)
    parsed = docs.read_front_matter(first)
    assert parsed[1] is None
    charge = charge_of(first, "0.md")
    assert docs_cache.MAX_ENTRY_BYTES * 0.9 < charge <= docs_cache.MAX_ENTRY_BYTES
    count = docs_cache.MAX_BYTES // charge + 4
    for i in range(1, count):
        write(root / f"{i}.md", text)
    # A near-YAML_MAX_NODES parse takes most of a second; the store only sees the value.
    monkeypatch.setattr(docs, "read_front_matter", lambda path: (deepcopy(parsed[0]), None))
    for i in range(count):
        docs_cache.read(root, f"{i}.md", root / f"{i}.md")
        assert docs_cache.stats()["bytes"] <= docs_cache.MAX_BYTES
    assert 0 < docs_cache.stats()["entries"] < count


def test_keys_forget_one_root(tmp_path, aged):
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (pa,) = _files(root_a, 1)
    (pb,) = _files(root_b, 1)
    docs_cache.read(root_a, "f0.md", pa)
    docs_cache.read(root_b, "f0.md", pb)
    assert docs_cache.stats()["entries"] == 2
    docs_cache.forget(root_a)
    assert _keys(root_a) == []
    assert _keys(root_b) == ["f0.md"]
    assert docs_cache.stats()["bytes"] == live_charges()


def test_keys_normalised(tmp_path, aged, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "x").mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (p,) = _files(repo, 1)
    before = docs_cache.stats()
    docs_cache.read(Path("x/../repo"), "f0.md", p)
    docs_cache.read(Path("./repo"), "f0.md", p)
    assert delta(before) == {"hits": 1, "misses": 1, "fresh": 0}
    docs_cache.forget(Path("repo/"))
    assert docs_cache.stats()["entries"] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("read_via, forget_via", [("link", "real"), ("real", "link")])
def test_keys_resolve_symlinked_root(tmp_path, aged, read_via, forget_via):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    (p,) = _files(real, 1)
    roots = {"real": real, "link": link}
    docs_cache.read(roots[read_via], "f0.md", p)
    assert docs_cache.stats()["entries"] == 1
    docs_cache.forget(roots[forget_via])
    assert docs_cache.stats()["entries"] == 0


def test_bounds_charge_long_rel_keys(tmp_path, aged, monkeypatch):
    rels = [f"{i}-" + "d/" * 1995 + "f.md" for i in range(10)]
    paths = _files(tmp_path, 10)
    one = charge_of(paths[0], rels[0])
    assert one > docs_cache.ENTRY_OVERHEAD + sys.getsizeof(rels[0])
    monkeypatch.setattr(docs_cache, "MAX_BYTES", one * 3 + one // 2)
    for rel, p in zip(rels, paths):
        docs_cache.read(tmp_path, rel, p)
        assert docs_cache.stats()["bytes"] <= docs_cache.MAX_BYTES
    assert _keys(tmp_path) == rels[-3:]
    assert docs_cache.stats()["bytes"] == live_charges()


class _OwnedLock:
    """A lock whose `locked()` answers for the calling thread, so spies can run concurrently."""

    def __init__(self):
        self._lock = threading.Lock()
        self._owner = None

    def __enter__(self):
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self._owner = None
        self._lock.release()

    def locked(self):
        return self._owner == threading.get_ident()


def test_threads(tmp_path, aged, monkeypatch):
    paths = _files(tmp_path, 200)
    expected = {p: docs.read_front_matter(p) for p in paths}
    monkeypatch.setattr(docs_cache, "_lock", _OwnedLock())
    unlocked = []

    def guard(name, real):
        def spy(*args, **kwargs):
            unlocked.append(not docs_cache._lock.locked())
            return real(*args, **kwargs)

        return spy

    monkeypatch.setattr(docs, "read_front_matter", guard("parse", docs.read_front_matter))
    monkeypatch.setattr(docs_cache, "deepcopy", guard("deepcopy", docs_cache.deepcopy))
    monkeypatch.setattr(docs_cache, "_deep_size", guard("deep_size", docs_cache._deep_size))
    before = docs_cache.stats()
    errors = []
    rounds = 3
    fresh_every = 5

    def worker(seed, fresh):
        order = list(paths)
        random.Random(seed).shuffle(order)
        for _ in range(rounds):
            for i, p in enumerate(order):
                if docs_cache.read(tmp_path, p.name, p) != expected[p]:
                    errors.append(p)
                if fresh and i % fresh_every == 0 and docs_cache.read_fresh(tmp_path, p.name, p) != expected[p]:
                    errors.append(p)

    # Half the threads also read_fresh every fifth file, racing the lookups and stores.
    threads = [threading.Thread(target=worker, args=(i, i % 2 == 0)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert unlocked and all(unlocked)
    d = delta(before)
    assert d["hits"] + d["misses"] == 8 * rounds * len(paths)
    assert d["fresh"] == 4 * rounds * len(range(0, len(paths), fresh_every))
    assert docs_cache.stats()["entries"] == len(paths)
    assert docs_cache.stats()["bytes"] == live_charges()


@NO_CTIME
def test_forward_clock_fuzz(tmp_path, aged):
    rng = random.Random(1234)
    root = tmp_path / "repo"
    targets = tmp_path / "targets"
    root.mkdir()
    targets.mkdir()
    target_paths = []
    for i in range(4):
        t = targets / f"t{i}.md"
        write(t, doc(f"TGT-{i:04d}"))
        target_paths.append(t)
    st = os.stat(target_paths[0])
    for t in target_paths:
        os.utime(t, ns=(st.st_atime_ns, st.st_mtime_ns))
    paths = _files(root, 20)
    counter = iter(range(1000, 100_000))

    def settle(p):
        wait_ctime_advance(root / "f0.md", os.stat(p).st_ctime_ns if os.path.exists(p) else 0)

    def writable(p):
        if os.path.exists(p):
            os.chmod(p, 0o644)

    for _ in range(500):
        p = rng.choice(paths)
        step = rng.choice(["same_size", "resize", "replace", "delete", "recreate", "retarget", "chmod", "noop"])
        if step == "same_size" and os.path.exists(p):
            writable(p)
            old = os.stat(p)
            write(p, doc(f"ADR-{next(counter):05d}"))
            os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))
            settle(p)
        elif step == "resize" and os.path.exists(p):
            writable(p)
            write(p, doc(f"ADR-{next(counter):05d}", "x: " + "y" * rng.randrange(1, 40) + "\n"))
            settle(p)
        elif step == "replace":
            old = os.stat(p) if os.path.exists(p) else None
            tmp = root / ".tmp"
            write(tmp, doc(f"ADR-{next(counter):05d}"))
            if old is not None:
                os.utime(tmp, ns=(old.st_atime_ns, old.st_mtime_ns))
            os.replace(tmp, p)
            settle(p)
        elif step == "delete" and os.path.lexists(p):
            p.unlink()
            settle(root / "f0.md")
        elif step == "recreate" and not os.path.lexists(p):
            write(p, doc(f"ADR-{next(counter):05d}"))
            settle(p)
        elif step == "retarget":
            tmp = root / ".link"
            if os.path.lexists(tmp):
                tmp.unlink()
            tmp.symlink_to(rng.choice(target_paths))
            os.replace(tmp, p)
            settle(p)
        elif step == "chmod" and os.path.exists(p):
            os.chmod(p, rng.choice([0, 0o644, 0o600]))
            settle(p)
        for q in paths:
            assert docs_cache.read(root, q.name, q) == docs.read_front_matter(q), (step, q)
        q = rng.choice(paths)
        assert docs_cache.read_fresh(root, q.name, q) == docs.read_front_matter(q), (step, q)
    for q in paths + target_paths:
        if os.path.exists(q):
            os.chmod(q, 0o644)


def test_coarse_stat_fuzz(tmp_path, monkeypatch):
    rng = random.Random(4321)
    clock = [time.time_ns()]
    real_stat = os.stat

    def coarse(path):
        st = real_stat(path)
        mtime = st.st_mtime_ns // (2 * SECOND) * (2 * SECOND)
        return SimpleNamespace(
            st_dev=st.st_dev, st_ino=st.st_ino, st_size=st.st_size, st_mtime_ns=mtime, st_ctime_ns=mtime
        )

    monkeypatch.setattr(docs_cache, "_stat", coarse)
    monkeypatch.setattr(docs_cache, "_clock", lambda: clock[0])
    paths = _files(tmp_path, 10)
    for p in paths:
        os.utime(p, ns=(clock[0], clock[0]))
    counter = iter(range(1000, 100_000))
    for _ in range(500):
        clock[0] += rng.randrange(0, 2_500_000_001)
        p = rng.choice(paths)
        step = rng.choice(["same_size", "resize", "replace", "noop"])
        if step == "same_size":
            write(p, doc(f"ADR-{next(counter):05d}"))
        elif step == "resize":
            write(p, doc(f"ADR-{next(counter):05d}", "x: " + "y" * rng.randrange(1, 40) + "\n"))
        elif step == "replace":
            tmp = tmp_path / ".tmp"
            write(tmp, doc(f"ADR-{next(counter):05d}"))
            os.replace(tmp, p)
        if step != "noop":
            os.utime(p, ns=(clock[0], clock[0]))
        for q in paths:
            assert docs_cache.read(tmp_path, q.name, q) == docs.read_front_matter(q), step
    assert docs_cache.stats()["hits"] > 0
