"""Contract tests for the edge cache key module.

Each test calls one of the five public stubs in ``edgecache.keys``.  With the
stubs in place the suite reports exactly 15 failures and no collection or
import errors.  Only the standard library and pytest are used.
"""

import threading

import pytest

from edgecache.keys import (
    InvalidKey,
    cache_key,
    coalesce,
    normalize_query,
    tenant_scope,
    vary_key,
)

POLICY = {"vary": ["Accept-Encoding"], "tenant_header": "X-Tenant"}


def make_request(**overrides):
    """Build a request dict; overrides replace top-level fields."""
    request = {
        "method": "GET",
        "scheme": "https",
        "host": "example.com",
        "path": "/",
        "query": "",
        "headers": {"X-Tenant": "acme", "Accept-Encoding": "gzip"},
    }
    request.update(overrides)
    return request


# --- Defect 1: raw query strings fragment the cache; key material is bounded. -


def test_query_param_order_same_key():
    first = make_request(query="b=2&a=1")
    second = make_request(query="a=1&b=2")
    assert cache_key(first, POLICY) == cache_key(second, POLICY)


def test_duplicate_params_preserved_order_stable():
    assert normalize_query("a=1&a=2&a=1") == [
        ("a", "1"),
        ("a", "2"),
        ("a", "1"),
    ]
    assert normalize_query("b=2&a=1&a=3") == [
        ("a", "1"),
        ("a", "3"),
        ("b", "2"),
    ]


def test_empty_value_preserved():
    assert normalize_query("a=&b=2") == [("a", ""), ("b", "2")]
    assert normalize_query("a=") == [("a", "")]


def test_oversized_key_rejected():
    request = make_request(query="q=" + "x" * 3000)
    with pytest.raises(InvalidKey):
        cache_key(request, POLICY)


# --- Defect 2: percent encoding is not canonicalised. -----------------------


def test_percent_encoded_letter_same_key():
    assert normalize_query("%41=1") == [("A", "1")]
    assert normalize_query("%2541=1") == [("%2541", "1")]
    assert normalize_query("a=%2f") == [("a", "%2F")]
    assert cache_key(make_request(query="%41=1"), POLICY) == cache_key(
        make_request(query="A=1"), POLICY
    )


def test_plus_and_percent20_same_key():
    assert normalize_query("q=a+b") == [("q", "a%20b")]
    assert normalize_query("q=a%20b") == [("q", "a%20b")]
    assert cache_key(make_request(query="q=a+b"), POLICY) == cache_key(
        make_request(query="q=a%20b"), POLICY
    )


def test_control_char_key_rejected():
    for bad in ("%00=1", "%0A=1", "%1F=1", "%7F=1", "\x00=1", "\x7f=1"):
        with pytest.raises(InvalidKey):
            normalize_query(bad)


# --- Defect 3: host and path spelling poisons another resource's key. -------


def test_host_case_normalized():
    plain = make_request(host="example.com")
    assert cache_key(make_request(host="EXAMPLE.COM"), POLICY) == cache_key(
        plain, POLICY
    )

    # The Host header must not override request["host"].
    spoofed = make_request(
        host="example.com",
        headers={
            "X-Tenant": "acme",
            "Accept-Encoding": "gzip",
            "Host": "victim.example.net",
        },
    )
    assert cache_key(spoofed, POLICY) == cache_key(plain, POLICY)

    assert cache_key(make_request(host="B\u00dcCHER.example"), POLICY) == cache_key(
        make_request(host="xn--bcher-kva.example"), POLICY
    )


def test_host_trailing_dot_removed():
    plain = cache_key(make_request(host="example.com"), POLICY)
    for host in ("example.com.", "Example.COM.", "example.com.."):
        assert cache_key(make_request(host=host), POLICY) == plain


def test_double_slash_merged():
    plain = cache_key(make_request(path="/a/b/c"), POLICY)
    for path in ("/a//b///c", "//a/b/c", "/a/./b/c"):
        assert cache_key(make_request(path=path), POLICY) == plain


# --- Defect 4: tenant and Vary scope are missing from the key. --------------


def test_tenant_header_missing_rejected():
    request = make_request(headers={"Accept-Encoding": "gzip"})
    with pytest.raises(InvalidKey):
        tenant_scope(request, POLICY)
    with pytest.raises(InvalidKey):
        cache_key(request, POLICY)
    with pytest.raises(InvalidKey):
        tenant_scope(make_request(), {"vary": []})


def test_tenant_header_invalid_rejected():
    for bad in ("Acme", "acme_1", "", "a" * 33, "acme/x", " acme"):
        request = make_request(
            headers={"X-Tenant": bad, "Accept-Encoding": "gzip"}
        )
        with pytest.raises(InvalidKey):
            tenant_scope(request, POLICY)


def test_different_tenant_different_key():
    policy = {"vary": ["Accept-Encoding"], "tenant_header": "x-tenant"}
    acme = make_request(
        headers={"X-Tenant": "acme", "Accept-Encoding": "gzip"}
    )
    globex = make_request(
        headers={"X-Tenant": "globex", "Accept-Encoding": "gzip"}
    )
    assert tenant_scope(acme, policy) == "acme"
    assert vary_key(acme, policy) == ("gzip",)
    assert cache_key(acme, policy) != cache_key(globex, policy)


# --- Defect 5: concurrent misses hit the origin more than once. -------------


class _CountingInflight(dict):
    """Registry that records distinct waiter threads which read a live entry.

    Waiters must find the leader's entry with a dict read (``get`` or ``[]``).
    Counting those reads lets the test release the blocked origin call only
    after every waiter has joined the same flight, without sleep-based racing.
    """

    def __init__(self):
        super().__init__()
        self._condition = threading.Condition()
        self._waiter_idents = set()
        self._leader_ident = None

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        self._leader_ident = threading.get_ident()

    def mark_leader(self):
        self._leader_ident = threading.get_ident()

    def _note_lookup(self, key):
        if not dict.__contains__(self, key):
            return
        ident = threading.get_ident()
        if ident == self._leader_ident:
            return
        with self._condition:
            self._waiter_idents.add(ident)
            self._condition.notify_all()

    def get(self, key, default=None):
        value = dict.get(self, key, default)
        if dict.__contains__(self, key):
            self._note_lookup(key)
        return value

    def __getitem__(self, key):
        value = dict.__getitem__(self, key)
        self._note_lookup(key)
        return value

    def wait_for_waiters(self, count, timeout):
        with self._condition:
            return self._condition.wait_for(
                lambda: len(self._waiter_idents) >= count,
                timeout=timeout,
            )


def test_coalesce_single_flight():
    inflight = _CountingInflight()
    release = threading.Event()
    factory_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    sentinel = object()

    def factory():
        nonlocal calls
        inflight.mark_leader()
        with calls_lock:
            calls += 1
        factory_started.set()
        if not release.wait(timeout=5):
            raise AssertionError("origin factory was not released")
        return sentinel

    barrier = threading.Barrier(8)
    results = []
    failures = []

    def worker():
        try:
            barrier.wait(timeout=5)
            results.append(coalesce("same-key", inflight, factory))
        except BaseException as exc:  # noqa: BLE001 - surface every thread error
            failures.append(exc)

    threads = [
        threading.Thread(target=worker, name=f"coalesce-{index}", daemon=True)
        for index in range(8)
    ]
    for thread in threads:
        thread.start()

    started = factory_started.wait(timeout=1.0)
    joined = started and inflight.wait_for_waiters(7, timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert started, "the origin factory was never called"
    assert joined, "concurrent callers did not wait on the in-flight entry"
    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert calls == 1
    assert len(results) == 8
    assert all(result is sentinel for result in results)
    assert "same-key" not in inflight


def test_coalesce_exception_cleans_inflight():
    inflight = _CountingInflight()
    release = threading.Event()
    factory_started = threading.Event()
    calls = 0
    calls_lock = threading.Lock()
    boom = RuntimeError("origin exploded")

    def factory():
        nonlocal calls
        inflight.mark_leader()
        with calls_lock:
            calls += 1
        factory_started.set()
        if not release.wait(timeout=5):
            raise AssertionError("origin factory was not released")
        raise boom

    barrier = threading.Barrier(8)
    caught = []
    unexpected = []

    def worker():
        try:
            barrier.wait(timeout=5)
            coalesce("same-key", inflight, factory)
        except RuntimeError as exc:
            caught.append(exc)
        except BaseException as exc:  # noqa: BLE001 - surface every thread error
            unexpected.append(exc)

    threads = [
        threading.Thread(target=worker, name=f"coalesce-{index}", daemon=True)
        for index in range(8)
    ]
    for thread in threads:
        thread.start()

    started = factory_started.wait(timeout=1.0)
    joined = started and inflight.wait_for_waiters(7, timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert started, "the origin factory was never called"
    assert joined, "concurrent callers did not wait on the in-flight entry"
    assert all(not thread.is_alive() for thread in threads)
    assert unexpected == []
    assert calls == 1
    assert len(caught) == 8
    assert all(
        isinstance(exc, RuntimeError) and str(exc) == "origin exploded"
        for exc in caught
    )
    assert "same-key" not in inflight
