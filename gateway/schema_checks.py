"""Reuse published JSON Schema validators while bounding retained descriptors."""

import json
from collections import OrderedDict

from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry


class SchemaChecks:
    """Compile lazily per schema object and validate every instance with no remote registry."""

    def __init__(self, limit: int = 128, byte_limit: int = 2 * 1024 * 1024):
        """Bound both the entry count and serialized weight of retained schemas."""
        self._limit = limit
        self._byte_limit = byte_limit
        self._weight = 0
        self._compiled: OrderedDict[int, tuple[Validator, int]] = OrderedDict()
        self._registry = Registry()

    def validate(self, instance, schema: dict) -> None:
        """Check the metaschema once per cache entry and retain a strong schema reference."""
        key = id(schema)
        cached = self._compiled.get(key)
        if cached is not None:
            self._compiled.move_to_end(key)
            compiled, _ = cached
        else:
            validator = validator_for(schema)
            validator.check_schema(schema)
            compiled = validator(schema, registry=self._registry)
            weight = len(json.dumps(schema, separators=(",", ":"), ensure_ascii=True))
            if weight <= self._byte_limit:
                self._compiled[key] = compiled, weight
                self._weight += weight
                while len(self._compiled) > self._limit or self._weight > self._byte_limit:
                    _, (_, removed) = self._compiled.popitem(last=False)
                    self._weight -= removed
        compiled.validate(instance)
