import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, maxsize: int = 2048, ttl: float = 600):
        self.maxsize = maxsize
        self.ttl = ttl
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: Any) -> Any | None:
        item = self._data.get(key)
        if item is None or item[0] < time.monotonic():
            if item is not None:
                del self._data[key]
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return item[1]

    def set(self, key: Any, value: Any) -> None:
        if self.ttl <= 0 or self.maxsize <= 0:
            return
        self._data[key] = (time.monotonic() + self.ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def stats(self) -> dict[str, int]:
        return {"size": len(self._data), "hits": self.hits, "misses": self.misses}
