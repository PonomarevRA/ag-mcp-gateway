"""Derive searchable capability labels without retaining MCP payloads."""

import asyncio
import re
from collections import Counter, OrderedDict, deque
from itertools import islice
from time import monotonic

# Only these known technical concepts may be learned from live payload keys.
_CONCEPTS = {
    "trace": {"trace", "traces", "traceid", "traceparent", "tracing", "трассировка", "трассировки"},
    "logs": {"log", "logs", "logging", "logline", "логи", "логов", "лог"},
    "metrics": {"metric", "metrics", "prometheus", "promql", "метрика", "метрики"},
    "grafana": {"grafana", "loki", "logql"},
    "opensearch": {"opensearch", "elasticsearch", "kibana"},
    "dashboard": {"dashboard", "dashboards", "дашборд", "дашборды"},
}
_ALIAS = {alias: concept for concept, aliases in _CONCEPTS.items() for alias in aliases}
_WORD = re.compile(r"[^\W_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEYWORD_LIMIT = 24
_NODE_LIMIT = 96
_PENDING_LIMIT = 256
_TOOL_LIMIT = 1024
_OBSERVATION_TTL = 3600


def words(text: str) -> set[str]:
    """Normalize qualified names, camelCase fields and aliases for search."""
    sample = text[:1200]
    normalized = _WORD.findall(sample.casefold()) + _WORD.findall(_CAMEL.sub(" ", sample).casefold())
    return {word for word in normalized if 1 <= len(word) <= 40}


def concepts(terms: set[str]) -> set[str]:
    """Map aliases and split trace-id spelling to stable technical concepts."""
    return {_ALIAS[term] for term in terms if term in _ALIAS}


def observed_concepts(document) -> set[str]:
    """Inspect a bounded runtime object shape and ignore all scalar values."""
    pending = [document]
    labels = set()
    for _ in range(_NODE_LIMIT):
        if not pending:
            break
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in islice(item.items(), 24):
                labels.update(concepts(words(key)))
                if isinstance(child, (list, dict)):
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(item[:4])
    return labels


def schema_terms(document) -> set[str]:
    """Collect schema property names through common JSON Schema containers."""
    pending = [document]
    terms = set()
    for _ in range(_NODE_LIMIT):
        if not pending:
            break
        schema = pending.pop()
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, child in islice(properties.items(), 48):
                terms.update(words(name))
                pending.append(child)
        pending.append(schema.get("items"))
        definitions = schema.get("$defs")
        if isinstance(definitions, dict):
            pending.extend(islice(definitions.values(), 8))
        for keyword in ("anyOf", "oneOf", "allOf"):
            variants = schema.get(keyword)
            if isinstance(variants, list):
                pending.extend(variants[:4])
    return terms


def catalog_terms(name, tool) -> tuple[set[str], set[str]]:
    """Separate specific schema/name terms from descriptive words."""
    specific = words(f"{name} {tool.title or ''}")
    specific |= schema_terms(tool.inputSchema) | schema_terms(tool.outputSchema)
    return specific, words(tool.description or "")


class ToolKeywords:
    """Keep a bounded, per-profile queue and counts of fixed capability labels."""

    def __init__(self):
        """Start a fresh profile-local observation window."""
        self._pending = deque(maxlen=_PENDING_LIMIT)
        self._observed: OrderedDict[str, tuple[float, Counter]] = OrderedDict()

    def observe(self, name: str, arguments: dict, result: dict | None) -> None:
        """Queue only technical labels extracted from the shape of a successful call."""
        if len(self._pending) == _PENDING_LIMIT:
            return
        labels = observed_concepts(arguments) | observed_concepts(result)
        if labels:
            self._pending.append((name, labels))

    def collect(self) -> None:
        """Coalesce a bounded batch and expire least recently observed tool names."""
        now = monotonic()
        while self._observed:
            oldest, (expires, _) = next(iter(self._observed.items()))
            if expires > now:
                break
            del self._observed[oldest]
        while self._pending:
            name, labels = self._pending.popleft()
            previous = self._observed.get(name)
            counts = previous[1] if previous else Counter()
            for label in labels:
                counts[label] = min(65535, counts[label] + 1)
            self._observed[name] = (now + _OBSERVATION_TTL, counts)
            self._observed.move_to_end(name)
            if len(self._observed) > _TOOL_LIMIT:
                self._observed.popitem(last=False)

    def observed(self, name: str) -> tuple[str, ...]:
        """Read current runtime labels in decreasing observation frequency."""
        entry = self._observed.get(name)
        if entry is None or entry[0] <= monotonic():
            return ()
        counts = entry[1]
        return tuple(sorted(counts, key=lambda label: (-counts[label], label)))

    async def run(self) -> None:
        """Batch enrichment while the profile is running."""
        while True:
            await asyncio.sleep(1)
            self.collect()

    def describe(self, name, tool) -> dict:
        """Return descriptor terms, runtime labels and suggested follow-up searches."""
        specific, description = catalog_terms(name, tool)
        topics = concepts(specific | description)
        specific -= {"get", "list", "tool", "string", "object", "array", "boolean", "number"}
        published = list(dict.fromkeys((*sorted(topics), *sorted(specific), *sorted(description))))[:_KEYWORD_LIMIT]
        observed = list(self.observed(name))
        backend = name.partition(".")[0] if "." in name else ""
        related = [f"{backend} {topic}" if backend and backend != topic else topic
                   for topic in sorted(topics | set(observed))]
        return {"keywords": published, "observed_keywords": observed, "related_queries": related}
