"""Invented Claude Code requests pin wire bytes, all blocks, and lane prefix stability.

Lite layout projections: codex-lb/tests/fixtures/codex_bodies, changed;
Copyright (c) 2025 Soju06 (MIT), permission notice in THIRD_PARTY_NOTICES.md.
"""
import copy
import dataclasses
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import fakes
from gpt_gateway.chatgpt import catalog, request, signature, tools
from tests.gpt_gateway.chatgpt import test_route as route_tests

FIXTURES = Path(__file__).parent / 'fixtures'


class RequestTest(unittest.TestCase):
    def setUp(self):
        self.model = catalog.Model('gpt-example', 272000, ('minimal', 'low', 'medium', 'high', 'xhigh'),
                                   'medium', 'low', 'auto', False, ('priority',))

    def result(self, body, **model_changes):
        translation = request.translate(body, dataclasses.replace(self.model, **model_changes), 'lane-example')
        return json.loads(translation.body_bytes), translation

    def test_all_blocks_golden_and_field_order(self):
        body = json.loads((FIXTURES / 'request_all_blocks.json').read_text())
        before = copy.deepcopy(body)
        translated = request.translate(body, self.model, 'lane-example')
        expected = (FIXTURES / 'responses_all_blocks.json').read_bytes().rstrip(b'\n')
        self.assertEqual(translated.body_bytes, expected)
        self.assertEqual(body, before)
        self.assertEqual(translated.estimate_chars,
                         len(expected.decode('utf-8')) - len('data:image/png;base64,AA=='))
        self.assertFalse(translated.lite)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            translated.lite = True

    def test_cache_retention_rides_right_after_the_cache_key_and_changes_nothing_else(self):
        body = json.loads((FIXTURES / 'request_all_blocks.json').read_text())
        plain = request.translate(body, self.model, 'lane-example')
        self.assertEqual(request.translate(body, self.model, 'lane-example', retention=None).body_bytes, plain.body_bytes)
        kept = json.loads(request.translate(body, self.model, 'lane-example', retention='24h').body_bytes)
        names = list(kept)
        self.assertEqual(names[names.index('prompt_cache_key') + 1], 'prompt_cache_retention')
        self.assertEqual(kept['prompt_cache_retention'], '24h')
        del kept['prompt_cache_retention']
        self.assertEqual(kept, json.loads(plain.body_bytes))  # the cache key included

    def test_lite_golden_and_empty_tools(self):
        body = json.loads((FIXTURES / 'request_lite.json').read_text())
        value, translated = self.result(body, slug='gpt-6-sol', responses_lite=True)
        self.assertEqual(translated.body_bytes, (FIXTURES / 'responses_lite.json').read_bytes().rstrip(b'\n'))
        self.assertTrue(translated.lite)
        observed = {'prefix': [{k: item[k] for k in ('type', 'role')} for item in value['input'][:2]],
                    'top_level_tools': 'tools' in value, 'parallel_tool_calls': value['parallel_tool_calls'],
                    'reasoning_context': value['reasoning']['context']}
        for fixture in json.loads((FIXTURES / 'lite_layouts.json').read_text()):
            with self.subTest(source=fixture['source']):
                self.assertEqual(observed, fixture['layout'])
        body['tools'] = []
        value, _ = self.result(body, slug='gpt-6-sol', responses_lite=True)
        self.assertEqual(value['input'][0]['role'], 'developer')
        self.assertEqual(value['input'][0]['type'], 'message')
        self.assertFalse(value['parallel_tool_calls'])

    def test_reasoning_replay_foreign_damage_switch_and_prefix(self):
        for sig, expected, note in [
                (signature.encode('gpt-example', 'rs_1', 'opaque'),
                 {'type': 'reasoning', 'id': 'rs_1', 'summary': [], 'encrypted_content': 'opaque'}, False),
                ('foreign', None, False), (signature.PREFIX + 'broken', None, True)]:
            body = {'messages': [{'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': '', 'signature': sig}]}]}
            value, translated = self.result(body)
            self.assertEqual(value.get('input'), [expected] if expected else [])
            self.assertEqual(bool(translated.notes), note)
        body = {'system': 'Continue carefully.', 'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'signature': signature.encode('old-slug', 'rs_old', 'opaque')},
                {'type': 'thinking', 'signature': signature.encode('old-slug', 'rs_old2', 'opaque')},
                {'type': 'text', 'text': 'Prior reply.'}]}, {'role': 'user', 'content': 'Continue.'}]}
        value, _ = self.result(body)
        self.assertIn('input', value)
        switches = [x for x in value['input'] if '<model_switch>' in json.dumps(x)]
        self.assertEqual(len(switches), 1)
        self.assertEqual(value['input'][-2], switches[0])
        self.assertEqual(switches[0]['role'], 'developer')
        body['messages'] += [{'role': 'assistant', 'content': 'New reply.'}, {'role': 'user', 'content': 'More.'}]
        later, _ = self.result(body)
        self.assertEqual(later['input'][:len(value['input'])], value['input'])

    def test_agent_contract_rides_the_system_developer_item_only(self):
        body = {'system': 'Review the package.', 'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'signature': signature.encode('old-slug', 'rs_old', 'opaque')},
                {'type': 'text', 'text': 'Prior reply.'}]}, {'role': 'user', 'content': 'Continue.'}]}
        bare = json.loads(request.translate(body, self.model, 'lane-example').body_bytes)
        value = json.loads(request.translate(body, self.model, 'lane-example', agent_contract=True).body_bytes)
        self.assertEqual(value['input'][0]['content'],
                         [{'type': 'input_text', 'text': 'Review the package.'},
                          {'type': 'input_text', 'text': request.AGENT_CONTRACT}])
        self.assertEqual(value['input'][1:], bare['input'][1:])
        switch = [x for x in value['input'] if '<model_switch>' in json.dumps(x)][0]
        self.assertNotIn(request.AGENT_CONTRACT, json.dumps(switch, ensure_ascii=False))
        self.assertIn('SendMessage', request.AGENT_CONTRACT)
        lite = json.loads(request.translate(dict(body, tools=[{'name': 'Read', 'input_schema': {'type': 'object'}}]),
                                            dataclasses.replace(self.model, slug='gpt-6-sol', responses_lite=True),
                                            'lane-example', agent_contract=True).body_bytes)
        self.assertEqual(lite['input'][1]['content'][-1]['text'], request.AGENT_CONTRACT)
        body['messages'] += [{'role': 'assistant', 'content': 'New reply.'}, {'role': 'user', 'content': 'More.'}]
        later = json.loads(request.translate(body, self.model, 'lane-example', agent_contract=True).body_bytes)
        self.assertEqual(later['input'][:len(value['input'])], value['input'])
        no_system = json.loads(request.translate({'messages': [{'role': 'user', 'content': 'Hi.'}]}, self.model,
                                                 'lane-example', agent_contract=True).body_bytes)
        self.assertNotIn(request.AGENT_CONTRACT, json.dumps(no_system, ensure_ascii=False))

    def test_prefix_survives_empty_thinking_and_later_tool_history(self):
        body = {'system': [{'type': 'text', 'text': 'Stable instructions.'}], 'messages': [
            {'role': 'user', 'content': 'First question.'}, {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'Reasoned once.',
                 'signature': signature.encode('gpt-example', 'rs_1', 'opaque')},
                {'type': 'text', 'text': 'First answer.'}]}, {'role': 'user', 'content': 'Inspect.'}]}
        for lite in (False, True):
            first, _ = self.result(body, responses_lite=lite)
            self.assertIn('input', first)
            later = copy.deepcopy(body)
            later['messages'][1]['content'][0]['thinking'] = ''
            later['messages'] += [
                {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_1', 'name': 'inspect', 'input': {'x': 1}}]},
                {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_1', 'content': 'Done.'}]}]
            second, _ = self.result(later, responses_lite=lite)
            prefix = json.dumps(first['input'], ensure_ascii=False, separators=(',', ':')).encode()[:-1]
            self.assertTrue(json.dumps(second['input'], ensure_ascii=False, separators=(',', ':')).encode().startswith(prefix))

    def test_effort_thresholds_caps_summary_verbosity_and_suffixes(self):
        cases = [({'output_config': {'effort': 'max'}}, 'xhigh'),
                 ({'output_config': {'effort': 'high'}, 'thinking': {'budget_tokens': 1}}, 'high'),
                 ({'thinking': {'budget_tokens': 4096}}, 'high'),
                 ({'thinking': {'budget_tokens': 2048}}, 'medium'),
                 ({'thinking': {'budget_tokens': 1024}}, 'low'),
                 ({'thinking': {'budget_tokens': 1023}}, 'minimal'), ({}, 'medium')]
        for extra, effort in cases:
            with self.subTest(extra=extra):
                value, _ = self.result(dict(model='gpt-example-fast[1m]', **extra))
                self.assertEqual(value.get('reasoning'), {'effort': effort, 'summary': 'auto'})
                self.assertEqual(value['model'], 'gpt-example')
                self.assertEqual(value['service_tier'], 'priority')
                self.assertEqual(value['text'], {'verbosity': 'low'})
                self.assertEqual(list(value)[:4], ['model', 'stream', 'service_tier', 'input'])
        value, _ = self.result({'output_config': {'effort': 'max'}}, reasoning_levels=('low', 'medium'))
        self.assertEqual(value.get('reasoning'), {'effort': 'medium', 'summary': 'auto'})
        value, _ = self.result({}, reasoning_summary='concise')
        self.assertEqual(value.get('reasoning'), {'effort': 'medium', 'summary': 'concise'})
        value, _ = self.result({}, reasoning_summary=None, default_verbosity=None, default_reasoning=None)
        self.assertEqual(value.get('reasoning'), {})
        self.assertEqual(value['text'], {})

    def test_a_pinned_effort_replaces_the_asked_one_within_the_catalog(self):
        # The route pins an activity-summary fork to its lane's effort; Translation.effort is what the body sends.
        for pinned, levels, sent in (('medium', ('low', 'medium', 'high'), 'medium'), (None, ('low', 'medium', 'high'), 'high'),
                                     ('xhigh', ('low', 'high'), 'high'), ('medium', (), None)):
            with self.subTest(pinned=pinned, levels=levels):
                translated = request.translate({'output_config': {'effort': 'high'}},
                                               dataclasses.replace(self.model, reasoning_levels=levels), 'lane-example', pinned)
                self.assertEqual((json.loads(translated.body_bytes)['reasoning'].get('effort'), translated.effort), (sent, sent))

    def test_activity_summary_keeps_its_agents_effort(self):
        # The backend's prompt cache keys on reasoning effort: a fork at another effort re-reads its lane's prefix uncached.
        ask = 'Describe your most recent action in 3-5 words using present tense (-ing). Do not use tools.'
        call = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]}]
        fork = call + [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': ''},
                                                    {'type': 'text', 'text': ask}, {'type': 'text', 'text': 'Be brief.'}]}]
        for extra, levels, effort in (({'output_config': {'effort': 'high'}}, ('minimal', 'low', 'high'), 'high'),
                                      ({'output_config': {'effort': 'xhigh'}}, ('xhigh', 'high', 'low'), 'xhigh'),
                                      ({}, ('medium', 'high'), 'medium')):
            with self.subTest(levels=levels):
                value, translated = self.result(dict(messages=fork, **extra), reasoning_levels=levels)
                self.assertEqual((value['reasoning']['effort'], translated.notes), (effort, ()))
        self.assertTrue(request.activity_summary({'messages': fork}))
        self.assertTrue(request.activity_summary({'messages': [{'role': 'user', 'content': ask}]}))
        # Claude Code appends harness notices as trailing system messages; the fork's ask sits before them.
        notice = {'role': 'system', 'content': 'SubagentStop hook feedback'}
        self.assertTrue(request.activity_summary({'messages': fork + [notice, notice]}))
        self.assertFalse(request.activity_summary({'messages': call + [{'role': 'system', 'content': ask}]}))
        # The ask is a summary only as the last user message's own text: quoted in a tool result, earlier or mid-text it is work.
        for messages in (call + [{'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'content': ask}]}],
                         [{'role': 'user', 'content': ask}, {'role': 'assistant', 'content': 'x'}, {'role': 'user', 'content': 'go'}],
                         [{'role': 'user', 'content': 'Please ' + ask}], [{'role': 'assistant', 'content': ask}], []):
            with self.subTest(messages=messages[-1:]):
                self.assertFalse(request.activity_summary({'messages': messages}))

    def test_tool_names_arguments_results_and_parallel(self):
        long = 'tool_' + 'a' * 90
        body = {'tool_choice': {'type': 'tool', 'name': long, 'disable_parallel_tool_use': True},
                'tools': [{'name': long, 'input_schema': {'type': 'object'}}], 'messages': [
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': long, 'input': {'city': 'Zürich'}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'c', 'is_error': True,
                                                'content': [{'type': 'text', 'text': 'Failed'},
                                                            {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': 'AA=='}}]}]}]}
        value, translated = self.result(body)
        self.assertEqual(translated.tool_names, {tools.short_name(long): long})
        self.assertEqual(value['input'][0]['name'], tools.short_name(long))
        self.assertEqual(value['input'][0]['arguments'], '{"city":"Zürich"}')
        self.assertEqual(value['input'][1]['output'], [
            {'type': 'input_text', 'text': '[tool execution error]'}, {'type': 'input_text', 'text': 'Failed'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,AA=='}])
        self.assertFalse(value['parallel_tool_calls'])
        value, _ = self.result({'messages': [{'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'lost', 'name': 'inspect', 'input': {}}]}]})
        self.assertEqual(value.get('input', [])[-1:], [{'type': 'function_call_output', 'call_id': 'lost', 'output': 'aborted'}])

    def test_structured_format_and_supported_images(self):
        body = {'output_config': {'format': {'type': 'json_schema', 'schema': {'type': 'object', 'properties': {}}}},
                'messages': [{'role': 'user', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': media, 'data': 'AA=='}}
                    for media in ('image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/svg+xml')]}]}
        value, _ = self.result(body)
        self.assertIn('text', value)
        self.assertEqual(value['text']['format'], {'type': 'json_schema', 'name': 'response', 'strict': True,
                                                  'schema': {'type': 'object', 'properties': {}}})
        self.assertEqual([x['type'] for x in value['input'][0]['content']], ['input_image'] * 4 + ['input_text'])
        # An image is priced flat per image by the estimator, so its payload never counts as characters.
        for nested in (False, True):
            sizes = set()
            for data in ('AA==', 'A' * 40000):
                image = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': data}}
                content = [{'type': 'tool_result', 'tool_use_id': 'c', 'content': [image]}] if nested else [image]
                call = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]}]
                sizes.add(self.result({'messages': call + [{'role': 'user', 'content': content}]})[1].estimate_chars)
            with self.subTest(nested=nested):
                self.assertEqual(len(sizes), 1, sizes)

    def test_pdf_documents_become_input_files(self):
        pdf = {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': 'JVBERi0x'}}
        part = {'type': 'input_file', 'filename': 'document.pdf', 'file_data': 'data:application/pdf;base64,JVBERi0x'}
        call = [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]}]
        value, _ = self.result({'messages': call + [{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'c', 'content': [{'type': 'text', 'text': 'PDF file read'}, pdf]}]}]})
        self.assertEqual(value['input'][1]['output'], [{'type': 'input_text', 'text': 'PDF file read'}, part])
        value, _ = self.result({'messages': [{'role': 'user', 'content': [pdf]}]})
        self.assertEqual(value['input'][0]['content'], [part])
        # Only a base64 PDF maps; any other document keeps its placeholder.
        for source in ({'type': 'base64', 'media_type': 'text/plain', 'data': 'aGk='},
                       {'type': 'text', 'media_type': 'text/plain', 'data': 'hi'},
                       {'type': 'url', 'url': 'https://example.invalid/a.pdf'}):
            with self.subTest(source=source['type'] + ' ' + source.get('media_type', '')):
                value, _ = self.result({'messages': [{'role': 'user', 'content': [{'type': 'document', 'source': source}]}]})
                self.assertEqual(value['input'][0]['content'], [{'type': 'input_text', 'text': '[unsupported document]'}])
        # A PDF is priced flat per attachment by the estimator, so its payload never counts as characters.
        for nested in (False, True):
            sizes = set()
            for data in ('AA==', 'A' * 40000):
                doc = dict(pdf, source=dict(pdf['source'], data=data))
                content = [{'type': 'tool_result', 'tool_use_id': 'c', 'content': [doc]}] if nested else [doc]
                sizes.add(self.result({'messages': call + [{'role': 'user', 'content': content}]})[1].estimate_chars)
            with self.subTest(nested=nested):
                self.assertEqual(len(sizes), 1, sizes)

    def test_unmapped_assistant_blocks_are_placeholders(self):
        value, _ = self.result({'messages': [{'role': 'assistant', 'content': [
            {'type': 'redacted_thinking', 'data': 'opaque'}, {'type': 'document'}]}]})
        self.assertEqual(value['input'], [{'type': 'message', 'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': '[unsupported redacted_thinking]'},
            {'type': 'output_text', 'text': '[unsupported document]'}]}])

    def deferred_body(self, loaded=True, referenced=('WebFetch',), tools_extra=()):
        """A Claude Code request with tool search on: the discovery call, its tool_reference result, the loaded tools."""
        fetch = {'name': 'WebFetch', 'description': 'Fetch a page.', 'input_schema': {'type': 'object'}, 'defer_loading': True}
        monitor = {'name': 'Monitor', 'description': 'Watch.', 'input_schema': {'type': 'object'}, 'defer_loading': True}
        placeholder = {'name': 'DeferredToolPlaceholder', 'description': 'Placeholder.', 'input_schema': {'type': 'object'},
                       'defer_loading': True}
        search = {'name': 'ToolSearch', 'description': 'Search tools.', 'input_schema': {'type': 'object'}}
        bash = {'name': 'Bash', 'description': 'Run.', 'input_schema': {'type': 'object'}}
        server = {'type': 'server_tool_20260301', 'name': 'remote', 'defer_loading': True}
        found = [{'type': 'tool_reference', 'tool_name': name} for name in referenced]
        messages = [{'role': 'user', 'content': 'Fetch the page.'},
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'ToolSearch',
                                                       'input': {'query': 'select:WebFetch'}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': found},
                                                 {'type': 'text', 'text': 'Tool loaded.'}]}]
        return {'tools': [bash, search, placeholder, server] + ([fetch, monitor] if loaded else []) + list(tools_extra),
                'messages': messages, 'system': 'Help.'}

    def test_deferred_tools_reach_the_model_after_the_result_that_names_them(self):
        lite = {'slug': 'gpt-6-sol', 'responses_lite': True}
        value, translated = self.result(self.deferred_body(), **lite)
        names = lambda item: [t['name'] for t in item['tools']]
        self.assertEqual([x['type'] for x in value['input']],
                         ['additional_tools', 'message', 'message', 'function_call', 'function_call_output',
                          'additional_tools', 'message'])
        self.assertEqual(names(value['input'][0]), ['Bash', 'ToolSearch'])
        self.assertEqual(value['input'][4]['output'], 'Tool loaded: WebFetch')
        self.assertEqual(names(value['input'][5]), ['WebFetch'])
        self.assertEqual(value['input'][5]['role'], 'developer')
        self.assertEqual(value['input'][6]['content'], [{'type': 'input_text', 'text': 'Tool loaded.'}])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())
        self.assertNotIn('Monitor', translated.body_bytes.decode())

    def test_the_first_item_is_the_same_before_and_after_a_discovery(self):
        lite = {'slug': 'gpt-6-sol', 'responses_lite': True}
        before = self.deferred_body(loaded=False)
        before['messages'] = before['messages'][:1]
        after = self.deferred_body(referenced=('WebFetch', 'Monitor'))
        first, _ = self.result(before, **lite)
        later, _ = self.result(after, **lite)
        self.assertEqual(first['input'][0], later['input'][0])
        self.assertEqual([t['name'] for t in later['input'][5]['tools']], ['WebFetch', 'Monitor'])
        self.assertEqual(later['input'][4]['output'], 'Tool loaded: WebFetch\nTool loaded: Monitor')
        self.assertEqual(first['input'][1:], later['input'][1:3])

    def test_a_tool_is_delivered_once_and_an_unlisted_or_inline_name_adds_nothing(self):
        lite = {'slug': 'gpt-6-sol', 'responses_lite': True}
        body = self.deferred_body(referenced=('WebFetch', 'Bash', 'Gone'))
        body['messages'] += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_2', 'name': 'ToolSearch',
                                                                'input': {}}]},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': [
                                 {'type': 'tool_reference', 'tool_name': 'WebFetch'}]}]}]
        value, _ = self.result(body, **lite)
        added = [x for x in value['input'] if x['type'] == 'additional_tools'][1:]
        self.assertEqual([[t['name'] for t in x['tools']] for x in added], [['WebFetch']])
        outputs = [x['output'] for x in value['input'] if x['type'] == 'function_call_output']
        self.assertEqual(outputs, ['Tool loaded: WebFetch\nTool loaded: Bash\nTool loaded: Gone', 'Tool loaded: WebFetch'])

    def test_a_deferred_tool_names_stay_reversible_and_a_model_without_lite_gets_them_in_tools(self):
        long = 'mcp__' + 'y' * 85
        extra = [{'name': long, 'description': 'Long.', 'input_schema': {'type': 'object'}, 'defer_loading': True}]
        body = self.deferred_body(referenced=(long,), tools_extra=extra)
        _, translated = self.result(body, slug='gpt-6-sol', responses_lite=True)
        self.assertEqual(translated.tool_names, {tools.short_name(long): long})
        value, _ = self.result(body)
        self.assertEqual([t['name'] for t in value['tools']],
                         ['Bash', 'ToolSearch', 'WebFetch', 'Monitor', tools.short_name(long)])
        self.assertEqual([x['type'] for x in value['input']],
                         ['message', 'message', 'function_call', 'function_call_output', 'message'])


    def addition_body(self, *additions, text='Docs connected.'):
        """A Claude Code request whose system message mid-conversation holds a text block and tool_addition blocks."""
        def addition(name, schema=None):
            return {'type': 'tool_addition', 'tool': {'type': 'tool_definition', 'definition': {
                'name': name, 'description': 'Does ' + name + '.', 'input_schema': schema or {'type': 'object'}}}}
        blocks = [{'type': 'text', 'text': text}] + [addition(*(a if isinstance(a, tuple) else (a,))) for a in additions]
        return {'tools': [{'name': 'Bash', 'description': 'Run.', 'input_schema': {'type': 'object'}}], 'system': 'Help.',
                'messages': [{'role': 'user', 'content': 'Start.'}, {'role': 'system', 'content': blocks},
                             {'role': 'user', 'content': 'Go on.'}]}

    def test_tool_additions_become_additional_tools_items_at_their_position(self):
        lite = {'slug': 'gpt-6-sol', 'responses_lite': True}
        schema = {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}
        body = self.addition_body(('docs_batch', schema), 'docs_guide')
        value, translated = self.result(body, **lite)
        self.assertEqual([x['type'] for x in value['input']],
                         ['additional_tools', 'message', 'message', 'message', 'additional_tools', 'additional_tools',
                          'message'])
        self.assertEqual(value['input'][3]['content'], [{'type': 'input_text', 'text': 'Docs connected.'}])
        self.assertEqual(value['input'][4], {'type': 'additional_tools', 'role': 'developer', 'tools': [
            {'type': 'function', 'name': 'docs_batch', 'description': 'Does docs_batch.', 'strict': False,
             'parameters': schema}]})
        self.assertEqual([t['name'] for t in value['input'][5]['tools']], ['docs_guide'])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())
        before = dict(body, messages=body['messages'][:1])
        first, _ = self.result(before, **lite)
        self.assertEqual(first['input'][:2], value['input'][:2])
        self.assertEqual(first['input'][0]['tools'], value['input'][0]['tools'])

    def test_tool_additions_in_a_model_without_lite_join_its_tools_and_leave_no_text(self):
        body = self.addition_body('docs_batch', 'docs_guide')
        body['messages'][1]['content'] = body['messages'][1]['content'][1:]
        value, translated = self.result(body)
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', 'docs_batch', 'docs_guide'])
        self.assertEqual([x['type'] for x in value['input']], ['message', 'message', 'message'])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())

    def test_a_tool_addition_is_delivered_once_stays_reversible_and_collides_loudly(self):
        long = 'mcp__' + 'z' * 85
        lite = {'slug': 'gpt-6-sol', 'responses_lite': True}
        body = self.addition_body(long, 'Bash', 'docs_batch')
        body['messages'].append(dict(body['messages'][1]))
        value, translated = self.result(body, **lite)
        added = [t['name'] for x in value['input'][1:] if x['type'] == 'additional_tools' for t in x['tools']]
        self.assertEqual(added, [tools.short_name(long), 'docs_batch'])
        self.assertEqual(translated.tool_names, {tools.short_name(long): long})
        clash = self.addition_body(long)
        clash['tools'].append({'name': tools.short_name(long), 'input_schema': {'type': 'object'}})
        with self.assertRaises(ValueError):
            self.result(clash, **lite)

    def test_a_malformed_tool_addition_stays_a_placeholder(self):
        for block in [{'type': 'tool_addition'}, {'type': 'tool_addition', 'tool': {'type': 'tool_definition',
                                                                                    'definition': {'name': ''}}}]:
            with self.subTest(block=block):
                body = {'messages': [{'role': 'system', 'content': [block]}]}
                value, _ = self.result(body, slug='gpt-6-sol', responses_lite=True)
                self.assertEqual(value['input'], [{'type': 'message', 'role': 'developer', 'content': [
                    {'type': 'input_text', 'text': '[unsupported tool_addition]'}]}])


class AgentContractRouteTest(unittest.TestCase):
    setUp = route_tests.RouteTest.setUp
    jwt = route_tests.RouteTest.jwt
    gateway = route_tests.RouteTest.gateway
    send = route_tests.RouteTest.send
    responses = route_tests.RouteTest.responses

    def test_the_route_sends_the_agent_contract_only_for_sub_agents_as_its_knob_says(self):
        for knob in ('on', 'off'):
            for subagent in (False, True):
                with self.subTest(knob=knob, subagent=subagent):
                    rt, _ = self.gateway(GPT_GATEWAY_CHATGPT_AGENT_CONTRACT=knob)
                    headers = self.headers if subagent else {'x-claude-code-session-id': 'session-invented'}
                    model = rt.chatgpt.catalog.models()['gpt-test']
                    self.fake.script = [(400, {'error': {'code': 'invalid_encrypted_content'}}),
                                        (200, fakes.responses_turn())]
                    before = len(self.responses())
                    # Exercise the translation owner directly: routing must not mask a main-session contract bug.
                    with patch('gpt_gateway.chatgpt.errors.backoff', return_value=0):
                        answer = rt.chatgpt.exchange(dict(self.body, system='Review it.', stream=False), model,
                                                    headers, '/v1/messages', Mock())
                    self.assertEqual(answer.status, 200)
                    answer.read()
                    attempts = self.responses()[before:]
                    self.assertEqual(len(attempts), 2)
                    for attempt in attempts:
                        sent = next(x for x in json.loads(attempt['body'])['input']
                                    if x['type'] == 'message' and x['role'] == 'developer')
                        parts = ['Review it.'] + ([request.AGENT_CONTRACT] if subagent and knob == 'on' else [])
                        self.assertEqual([part['text'] for part in sent['content']], parts)
