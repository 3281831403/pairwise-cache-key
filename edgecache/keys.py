"""Edge cache key contract and implementations.

The cache key must be canonical and tenant-scoped so two semantically
identical requests always map to the same key regardless of on-the-wire
spelling, while different tenants or Vary variants can never share one.

Only the Python standard library is needed.  No function performs network
I/O, uses the ``random`` module, or depends on wall-clock timing.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from hashlib import sha256
from typing import Callable
from urllib.parse import quote

__all__ = [
    "InvalidKey",
    "cache_key",
    "coalesce",
    "normalize_query",
    "tenant_scope",
    "vary_key",
]

# A percent escape followed by exactly two hexadecimal digits.
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
# Tenant identifiers are lowercase alphanumeric with single dashes.
_TENANT_RE = re.compile(r"^[a-z0-9-]{1,32}$")
# ASCII control characters: NUL..US plus DEL.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

# Bound on the UTF-8 length of canonical key material before hashing.
_MAX_KEY_MATERIAL = 2048


class InvalidKey(Exception):
    """Raised when a request cannot be mapped to a safe, canonical key."""


def _decode_component(raw: str) -> str:
    """Percent-decode one query component exactly once.

    ``+`` in a query string means a space.  Truncated or malformed percent
    escapes and invalid UTF-8 raise :class:`InvalidKey` instead of being
    silently passed through.
    """

    def replace(match: re.Match) -> str:
        return chr(int(match.group()[1:], 16))

    # Reject any literal '%' that does not begin a well-formed escape: after
    # every valid escape is removed, a stray '%' can only be a bad escape.
    stripped = _PERCENT_ESCAPE.sub("", raw)
    if "%" in stripped:
        raise InvalidKey(f"malformed percent escape in query component: {raw!r}")

    # Map valid escapes to their raw bytes first (via chr(0..255)), then turn
    # '+' into a space, so an encoded '+' (%2B) survives as a literal plus.
    decoded_like = _PERCENT_ESCAPE.sub(replace, raw).replace("+", " ")
    try:
        return decoded_like.encode("latin-1").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidKey("query component is not valid UTF-8") from exc


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
    pairs: list[tuple[str, str]] = []
    for segment in query.split("&"):
        if segment == "":
            continue
        if "=" in segment:
            raw_key, raw_value = segment.split("=", 1)
        else:
            raw_key, raw_value = segment, ""

        key = _decode_component(raw_key)
        if _CONTROL_RE.search(key):
            raise InvalidKey("query key contains an ASCII control character")
        value = _decode_component(raw_value)

        pairs.append((quote(key, safe=""), quote(value, safe="")))

    # Stable sort by canonical key only; equal keys keep their input order,
    # so duplicate parameters are preserved verbatim.
    pairs.sort(key=lambda pair: pair[0])
    return pairs


def _header(headers: dict, name: str) -> str | None:
    """Look up a header value case-insensitively."""
    wanted = name.lower()
    for header_name, value in headers.items():
        if header_name.lower() == wanted:
            return value
    return None


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
    header_name = policy.get("tenant_header")
    if not isinstance(header_name, str) or header_name == "":
        raise InvalidKey("policy tenant_header must be a non-empty string")

    value = _header(request.get("headers", {}), header_name)
    if value is None or value == "":
        raise InvalidKey(f"required tenant header {header_name!r} is missing")
    if not _TENANT_RE.fullmatch(value):
        raise InvalidKey(f"invalid tenant identifier: {value!r}")
    return value


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
    headers = request.get("headers", {})
    values: list[str] = []
    for name in policy.get("vary", []) or []:
        value = _header(headers, name)
        if value is None:
            values.append("")
        else:
            normalised = unicodedata.normalize("NFC", value)
            values.append(re.sub(r"\s+", " ", normalised).strip())
    return tuple(values)


def _normalize_host(host: str) -> str:
    """Lowercase, strip every trailing dot, and IDNA-encode *host*."""
    if not isinstance(host, str) or host == "":
        raise InvalidKey("request host is missing or empty")

    host = host.rstrip(".")
    if host == "":
        raise InvalidKey("request host is missing or empty")

    # Bracketed IPv6 literals have no DNS labels; lowercase them and leave
    # their contents otherwise untouched.
    if host.startswith("[") and host.endswith("]"):
        return host.lower()

    try:
        return host.encode("idna").decode("ascii").lower()
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise InvalidKey(f"host cannot be IDNA-encoded: {host!r}") from exc


def _normalize_path(path: str) -> str:
    """Apply RFC 3986 remove-dot-segments and collapse repeated slashes."""
    if not path.startswith("/"):
        path = "/" + path

    stack: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            # Empty segments are repeated slashes; "." is the current
            # directory.  Both contribute nothing.
            continue
        if segment == "..":
            if stack:
                stack.pop()
            # Above the root, ".." is simply discarded.
            continue
        stack.append(segment)
    return "/" + "/".join(stack)


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
    tenant = tenant_scope(request, policy)
    vary = vary_key(request, policy)

    scheme = request["scheme"].lower()
    host = _normalize_host(request["host"])
    path = _normalize_path(request["path"])
    query_pairs = normalize_query(request.get("query", ""))
    canonical_query = "&".join(f"{key}={value}" for key, value in query_pairs)

    material = "\n".join(
        [
            request["method"],
            scheme,
            host,
            path,
            canonical_query,
            tenant,
            # One positional field per declared Vary header.
            *vary,
        ]
    )

    encoded = material.encode("utf-8")
    if len(encoded) > _MAX_KEY_MATERIAL:
        raise InvalidKey(
            f"canonical key material is {len(encoded)} bytes, "
            f"over the {_MAX_KEY_MATERIAL}-byte limit"
        )

    return sha256(encoded).hexdigest()[:32]


class _Flight:
    """One in-flight origin fetch shared by every caller of a key."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._done = False
        self._result: object = None
        self._error: BaseException | None = None

    def complete(self, result: object) -> None:
        with self._condition:
            self._result = result
            self._done = True
            self._condition.notify_all()

    def fail(self, error: BaseException) -> None:
        with self._condition:
            self._error = error
            self._done = True
            self._condition.notify_all()

    def await_result(self) -> object:
        with self._condition:
            while not self._done:
                self._condition.wait()
            if self._error is not None:
                raise self._error
            return self._result


# Serialises the check-and-register decision only; the origin fetch itself
# runs without this lock held, so different keys never block each other.
_flight_lock = threading.Lock()


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
    with _flight_lock:
        flight = inflight.get(key)
        if flight is None:
            flight = _Flight()
            # Register before invoking factory so waiters always find the
            # entry with a plain dict read.
            inflight[key] = flight
            is_leader = True
        else:
            is_leader = False

    if not is_leader:
        return flight.await_result()

    try:
        result = factory()
        flight.complete(result)
        return result
    except BaseException as exc:
        flight.fail(exc)
        raise
    finally:
        with _flight_lock:
            inflight.pop(key, None)
