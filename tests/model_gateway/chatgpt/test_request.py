"""Invented Claude Code requests pin wire bytes, all blocks, and lane prefix stability.

Lite layout projections: codex-lb/tests/fixtures/codex_bodies, changed;
Copyright (c) 2025 Soju06 (MIT), permission notice in THIRD_PARTY_NOTICES.md.
"""
import copy
import dataclasses
import json
from pathlib import Path
import unittest

from model_gateway.chatgpt import catalog, request, signature, tools

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
