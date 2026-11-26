"""Explicit broker-symbol -> canonical symbol mapping (#29). No prefix or fuzzy matching:
an unmapped broker symbol is refused, so a quote for another instrument is never used."""


class SymbolMap:
    def __init__(self, mapping: dict[str, str]) -> None:
        self._map = dict(mapping)

    def canonical(self, broker_symbol: str) -> str | None:
        return self._map.get(broker_symbol)
