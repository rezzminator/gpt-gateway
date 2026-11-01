"""Invented schemas exercise the Codex policy and bridge compatibility rules."""
import copy
import unittest
from gpt_gateway.chatgpt.schema import sanitize


class SchemaTest(unittest.TestCase):
    def test_codex_lowering_table(self):
        cases = [(True, {'type': 'string'}), (False, {'type': 'string'}),
                 ({'description': 'Unspecified', 'title': 'Any'}, {'description': 'Unspecified', 'title': 'Any'}),
                 ({'type': 'any', 'description': 'Anything'}, {'description': 'Anything'}),
                 ({'type': ['unknown'], 'description': 'Anything'}, {'description': 'Anything'}),
                 ({'type': None, 'description': 'Anything'}, {'description': 'Anything'}),
                 ({'type': 'any', '$ref': '#/$defs/A'}, {'$ref': '#/$defs/A'}),
                 ({'type': ['unknown'], 'anyOf': [False]}, {'anyOf': [{'type': 'string'}]}),
                 ({'const': 7}, {'enum': [7], 'type': 'string'}),
                 ({'required': ['x']}, {'required': ['x'], 'type': 'object', 'properties': {}}),
                 ({'minimum': 1}, {'minimum': 1, 'type': 'number'}),
                 ({'type': ['unknown', 'array', 'null']}, {'type': ['array', 'null'], 'items': {'type': 'string'}}),
                 ({'type': ['object']}, {'type': 'object', 'properties': {}}),
                 ({'prefixItems': [False]}, {'prefixItems': [{'type': 'string'}], 'type': 'array', 'items': {'type': 'string'}}),
                 ({'anyOf': [False, {'const': 'x'}]}, {'anyOf': [{'type': 'string'}, {'enum': ['x'], 'type': 'string'}]}),
                 ({'$ref': '#/$defs/A', '$defs': {'A': True}}, {'$ref': '#/$defs/A', '$defs': {'A': {'type': 'string'}}}),
                 ({'type': 'object', 'additionalProperties': False, '$defs': []},
                  {'type': 'object', 'additionalProperties': False, 'properties': {}})]
        for original, expected in cases:
            with self.subTest(original=original):
                before = copy.deepcopy(original)
                self.assertEqual(sanitize(original), expected)
                self.assertEqual(original, before)
                self.assertEqual(sanitize(expected), expected)

    def test_schema_keywords_removed_without_corrupting_instance_data(self):
        literal = {'pattern': 'keep', '$id': 'keep', '$schema': 'keep'}
        original = {'type': 'object', '$id': 'drop', '$schema': 'drop', 'pattern': 'drop',
                    'properties': {'pattern': {'type': 'string', 'pattern': 'drop', 'default': literal}},
                    'if': {'properties': {'x': {'pattern': 'drop', 'type': 'string'}}},
                    'dependentSchemas': {'x': {'pattern': 'drop', 'type': 'object'}}}
        expected = {'type': 'object', 'properties': {'pattern': {'type': 'string', 'default': literal}},
                    'if': {'properties': {'x': {'type': 'string'}}},
                    'dependentSchemas': {'x': {'type': 'object'}}}
        self.assertEqual(sanitize(original), expected)
