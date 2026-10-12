"""Header policy: the secret headers, the hop-by-hop headers, and the read-only view hooks receive."""
from collections.abc import Mapping

SECRET = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie"})
HOP = frozenset({"host", "content-length", "connection", "accept-encoding", "transfer-encoding",
                 "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer", "upgrade"})


class Headers(Mapping):
    """A read-only, case-insensitive mapping of request headers without the secret ones.

    Keys are lower-cased; a header sent twice keeps its first value, as `Message.get` does.
    """

    __slots__ = ("_items",)

    def __init__(self, items):
        kept = {}
        for k, v in items:
            low = k.lower()
            if low not in SECRET and low not in kept:
                kept[low] = v
        object.__setattr__(self, "_items", kept)

    def __setattr__(self, name, value):
        raise AttributeError("Headers is read-only")

    def __getitem__(self, key):
        if not isinstance(key, str):
            raise KeyError(key)
        return self._items[key.lower()]

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def __repr__(self):
        return "Headers(%r)" % (self._items,)


def debug_headers(items):
    """The non-secret request headers the debug log records: x-*, anthropic-* and user-agent."""
    return {k: v for k, v in items
            if k.lower() not in SECRET
            and (k.lower().startswith(("x-", "anthropic-")) or k.lower() == "user-agent")}
