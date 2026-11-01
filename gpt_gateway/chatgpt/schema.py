"""Pure compatibility lowering of tool JSON schemas, preserving literal data.

Derived from openai/codex, changed (Apache-2.0). Pattern traversal distilled
from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT).
Permission notices: THIRD_PARTY_NOTICES.md.
"""
import copy

_TYPES = ('string', 'number', 'boolean', 'integer', 'object', 'array', 'null')
_TABLES = ('properties', 'patternProperties', '$defs', 'definitions', 'dependentSchemas', 'dependencies')
_CHILDREN = ('items', 'prefixItems', 'anyOf', 'allOf', 'oneOf', 'additionalProperties', 'additionalItems',
             'unevaluatedProperties', 'unevaluatedItems', 'contains', 'propertyNames', 'not', 'if', 'then',
             'else', 'contentSchema')


# codex-rs/tools/src/json_schema.rs:75-190; claude-code-proxy request.rs:721-770.
def sanitize(value):
    """Return a fresh lowered schema; defaults, enum values and property names are data."""
    result = copy.deepcopy(value)
    _strip_schema_metadata(result)
    return _lower_schema(result)


# claude-code-proxy/src/providers/codex/translate/request.rs:721-770 (expanded metadata keys).
def _strip_schema_metadata(value):
    if isinstance(value, list):
        for item in value:
            _strip_schema_metadata(item)
    elif isinstance(value, dict):
        for field in ('pattern', '$schema', '$id'):
            value.pop(field, None)
        for field in _TABLES:
            table = value.get(field)
            if isinstance(table, dict):
                for child in table.values():
                    _strip_schema_metadata(child)
        for field in _CHILDREN:
            if field in value:
                _strip_schema_metadata(value[field])


# codex-rs/tools/src/json_schema.rs:81-190,336-397; only schema-bearing children are lowered.
def _lower_schema(value):
    if isinstance(value, bool):
        return {'type': 'string'}
    if isinstance(value, list):
        return [_lower_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    for field in ('properties', '$defs', 'definitions'):
        if isinstance(value.get(field), dict):
            value[field] = {name: _lower_schema(child) for name, child in value[field].items()}
        elif field != 'properties':
            value.pop(field, None)
    for field in ('items', 'prefixItems', 'anyOf', 'oneOf', 'allOf', 'additionalProperties'):
        if field in value and not (field == 'additionalProperties' and isinstance(value[field], bool)):
            value[field] = _lower_schema(value[field])
    if 'const' in value:
        value['enum'] = [value.pop('const')]
    declared = value.get('type')
    declared = declared if isinstance(declared, list) else [declared]
    kinds = [kind for kind in declared if isinstance(kind, str) and kind in _TYPES]
    if not kinds:
        value.pop('type', None)  # a type naming nothing the backend knows is no type: upstream would reject it
        if any(field in value for field in ('$ref', 'anyOf', 'oneOf', 'allOf')):
            return value
        if any(field in value for field in ('properties', 'required', 'additionalProperties')):
            kinds = ['object']
        elif any(field in value for field in ('items', 'prefixItems')):
            kinds = ['array']
        elif any(field in value for field in ('enum', 'format')):
            kinds = ['string']
        elif any(field in value for field in ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf')):
            kinds = ['number']
        else:
            return value  # any value: no type to name, its description and other keys kept for the model
    value['type'] = kinds[0] if len(kinds) == 1 else kinds
    if 'object' in kinds:
        value.setdefault('properties', {})
    if 'array' in kinds:
        value.setdefault('items', {'type': 'string'})
    return value
