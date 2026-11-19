"""Edge cache key contract and stubs.

The five public functions below are intentionally unimplemented.  Every one
raises ``NotImplementedError`` so the contract tests in ``tests/test_keys.py``
start red; implement the functions to make the suite green.

Only the Python standard library is needed.  No function may perform network
I/O, use the ``random`` module, or depend on wall-clock timing.
"""

from __future__ import annotations

from typing import Callable

__all__ = [
    "InvalidKey",
    "cache_key",
    "coalesce",
    "normalize_query",
    "tenant_scope",
    "vary_key",
]


class InvalidKey(Exception):
    """Raised when a request cannot be mapped to a safe, canonical key."""


def normalize_query(query: str) -> list[tuple[str, str]]:
    """Canonicalise an HTTP query string for key construction.

    ``query`` is the raw query string without the leading ``?``.  Segments are
    separated by ``&`` and empty segments are ignored.  Contract:

    - Each key and value is percent-decoded exactly once.  In a query string
      ``+`` means a space.  A truncated or invalid escape, or invalid UTF-8,
      raises :class:`InvalidKey` instead of being passed through.
    - The decoded key and value are re-encoded with ``quote(..., safe="")``:
      spaces become ``%20``, hexadecimal digits are uppercase, and unreserved
      characters stay literal.  Consequently ``%41`` and ``A`` match, ``+``
      and ``%20`` match, and ``%2541`` is only decoded once.
    - Empty values are kept: ``"a="`` yields ``[("a", "")]``.  A segment with
      no ``=`` is treated as a key with an empty value.
    - Pairs are sorted by canonical key with a stable sort.  Every duplicate
      key is kept, and equal-key pairs retain their original relative order.
    - A key whose decoded text contains an ASCII control character
      (``U+0000``-``U+001F`` or ``U+007F``) raises :class:`InvalidKey`.
    - The returned tuples contain canonical, still percent-encoded text, so
      they can be joined directly to build key material.
    """
    raise NotImplementedError


def cache_key(request: dict, policy: dict) -> str:
    """Return a stable, tenant-scoped cache key for *request*.

    *request* is a dict with ``method``, ``scheme``, ``host``, ``path``,
    ``query``, and ``headers`` keys.  ``headers`` maps header names to string
    values.  Contract:

    - Scheme and host are lowercased.  The host is taken from
      ``request["host"]``; the ``Host`` header must never override it.  Remove
      every trailing dot and IDNA-encode non-ASCII labels (ASCII IP literals,
      including bracketed IPv6, stay lowercase).  A missing or empty host
      raises :class:`InvalidKey`.
    - Normalise the path: ensure a leading ``/``, collapse repeated ``/``,
      drop ``.`` segments, and resolve ``..`` without ever climbing above the
      root (RFC 3986 remove-dot-segments semantics).
    - Canonicalise ``request["query"]`` with :func:`normalize_query` and
      serialise the pairs as ``key=value`` joined by ``&`` in that order.
    - Include :func:`tenant_scope` and the values from :func:`vary_key` in the
      key material, so different tenants and Vary variants never share a key.
    - UTF-8 encode the canonical material, hash it with SHA-256, and return
      the first 32 hexadecimal characters.  If the material is longer than
      2048 bytes, raise :class:`InvalidKey`; never truncate silently.
    """
    raise NotImplementedError


def vary_key(request: dict, policy: dict) -> tuple[str, ...]:
    r"""Return the normalised Vary header values for *request*.

    ``policy["vary"]`` is a list of header names in declaration order.  An
    absent or empty list yields ``()``.  Contract:

    - Header lookup in ``request["headers"]`` is case-insensitive.
    - Exactly one tuple position is emitted for every declared name, in
      declaration order.  A missing header yields ``""`` so positions never
      shift between requests.
    - Values are Unicode NFC-normalised and whitespace-collapsed with
      ``re.sub(r"\s+", " ", value).strip()``.
    """
    raise NotImplementedError


def tenant_scope(request: dict, policy: dict) -> str:
    """Return the validated tenant identifier for *request*.

    ``policy["tenant_header"]`` names the header carrying the tenant.
    Contract:

    - A missing, empty, or non-string ``tenant_header`` policy value raises
      :class:`InvalidKey`; there is no default tenant.
    - The header lookup is case-insensitive.  A missing header raises
      :class:`InvalidKey`.
    - The value must match ``^[a-z0-9-]{1,32}$`` in full; otherwise raise
      :class:`InvalidKey`.  Return the validated value unchanged.
    """
    raise NotImplementedError


def coalesce(
    key: str,
    inflight: dict,
    factory: Callable[[], object],
) -> object:
    """Collapse concurrent misses for *key* into a single origin call.

    ``inflight`` is a caller-provided, dict-like registry shared by all
    callers; ``factory`` performs the origin fetch.  Contract:

    - The first caller registers an entry under ``key`` before invoking
      ``factory``.  Later callers find that entry with a dict read
      (``inflight.get(key)`` or ``inflight[key]``) and wait for the first
      caller instead of calling ``factory`` again.
    - ``factory`` runs exactly once per in-flight key.  Every caller for the
      same key, including the leader, receives the same result object.
    - If ``factory`` raises, store the exception in the entry, deliver it to
      every waiter, and remove the entry from ``inflight`` so a later caller
      can retry.
    - Always remove the entry in a ``finally`` path.  Once all callers have
      returned or raised, ``key`` is absent from ``inflight``.
    - Different keys use independent entries and never block each other.
    - The function must be safe to call concurrently from multiple threads.
    """
    raise NotImplementedError
