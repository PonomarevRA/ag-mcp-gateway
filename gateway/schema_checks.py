"""Reuse published JSON Schema validators while bounding retained descriptors."""

from collections import OrderedDict

from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry


class SchemaChecks:
    """Compile lazily per schema object and validate every instance with no remote registry."""

    def __init__(self, limit: int = 128):
        """Keep at most this many recently used published schema identities."""
        self._limit = limit
        self._compiled: OrderedDict[int, Validator] = OrderedDict()
        self._registry = Registry()

    def validate(self, instance, schema: dict) -> None:
        """Check the metaschema once per cache entry and retain a strong schema reference."""
        key = id(schema)
        compiled = self._compiled.get(key)
        if compiled is None:
            validator = validator_for(schema)
            validator.check_schema(schema)
            compiled = validator(schema, registry=self._registry)
            self._compiled[key] = compiled
            if len(self._compiled) > self._limit:
                self._compiled.popitem(last=False)
        else:
            self._compiled.move_to_end(key)
        compiled.validate(instance)
