"""Tool schema lowering, hosted tools, forced choices, and reversible names."""
import unittest
from model_gateway.chatgpt import tools


class ToolsTest(unittest.TestCase):
    def test_functions_hosted_and_dropped_server_tools(self):
        body = {'tools': [{'name': 'inspect', 'description': 'Inspect', 'input_schema': True},
                          {'type': 'web_search_20250305', 'name': 'web_search',
                           'allowed_domains': ['example.com'], 'blocked_domains': ['invalid.example']},
                          {'type': 'remote_20260101', 'name': 'remote'}]}
        converted, _, reverse, notes = tools.translate_tools(body)
        self.assertEqual(converted, [
            {'type': 'function', 'name': 'inspect', 'description': 'Inspect', 'strict': False,
             'parameters': {'type': 'string'}},
            {'type': 'web_search', 'external_web_access': True,
             'filters': {'allowed_domains': ['example.com'], 'blocked_domains': ['invalid.example']}}])
        self.assertEqual(reverse, {})
        self.assertTrue(any('remote_20260101' in note for note in notes))

    def test_long_names_and_choice_share_the_map(self):
        long = 'mcp__' + 'x' * 85
        short = tools.short_name(long)
        self.assertLessEqual(len(short), 64)
        self.assertTrue(short.startswith('mcp__'))
        self.assertNotEqual(short, tools.short_name(long + 'z'))
        self.assertEqual(tools.short_name('a' * 64), 'a' * 64)
        body = {'tools': [{'name': long, 'input_schema': {}}], 'tool_choice': {'type': 'tool', 'name': long}}
        converted, _, reverse, _ = tools.translate_tools(body)
        self.assertEqual(reverse, {short: long})
        self.assertEqual(converted[0]['name'], short)
        self.assertEqual(tools.tool_choice(body, reverse), {'type': 'function', 'name': short})

    def test_choice_table(self):
        for kind, expected in [('auto', 'auto'), ('none', 'none'), ('any', 'required'), ('required', 'required')]:
            for choice in (kind, {'type': kind}):
                with self.subTest(choice=choice):
                    self.assertEqual(tools.tool_choice({'tool_choice': choice}, {}), expected)
        body = {'tool_choice': {'type': 'tool', 'name': 'web_search'}}
        self.assertEqual(tools.tool_choice(body, {}), 'auto')
        body['tools'] = [{'type': 'web_search_20250305', 'name': 'web_search'}]
        self.assertEqual(tools.tool_choice(body, {}),
                         {'type': 'allowed_tools', 'mode': 'required', 'tools': [{'type': 'web_search'}]})

    def test_function_named_web_search_keeps_function_choice(self):
        body = {'tool_choice': {'type': 'tool', 'name': 'web_search'},
                'tools': [{'name': 'web_search', 'input_schema': {'type': 'object'}}]}
        self.assertEqual(tools.tool_choice(body, {}), {'type': 'function', 'name': 'web_search'})

    def test_defer_loading_tools_are_set_apart_in_order_and_keep_their_short_names(self):
        long = 'mcp__' + 'x' * 85
        body = {'tools': [{'name': 'inline', 'input_schema': {}},
                          {'name': 'late', 'input_schema': {}, 'defer_loading': True},
                          {'name': 'DeferredToolPlaceholder', 'input_schema': {}, 'defer_loading': True},
                          {'name': long, 'input_schema': {}, 'defer_loading': True},
                          {'name': 'off', 'input_schema': {}, 'defer_loading': False}]}
        converted, deferred, reverse, _ = tools.translate_tools(body)
        self.assertEqual([t['name'] for t in converted], ['inline', 'off'])
        self.assertEqual(list(deferred), ['late', long])
        self.assertEqual(deferred[long]['name'], tools.short_name(long))
        self.assertEqual(reverse, {tools.short_name(long): long})
        with self.assertRaises(ValueError):
            tools.translate_tools({'tools': [{'name': 'a', 'input_schema': {}},
                                             {'name': 'a', 'input_schema': {}, 'defer_loading': True}]})
