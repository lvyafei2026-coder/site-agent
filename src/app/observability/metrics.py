from __future__ import annotations


class Metrics:
    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def increment(self, name: str, amount: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + amount

    def snapshot(self) -> dict[str, int]:
        return dict(self._counters)


metrics = Metrics()
