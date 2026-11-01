"""Invented Claude Code requests pin wire bytes, all blocks, and lane prefix stability."""
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
                                   'medium', 'low', 'auto', ('priority',), support_verbosity=True)

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
        with self.assertRaises(dataclasses.FrozenInstanceError):
            translated.body_bytes = b"changed"

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

    def test_developer_identity_metadata_and_optional_delivery_controls(self):
        body = {'system': 'Stable instructions.', 'messages': [{'role': 'user', 'content': 'Hi'}]}
        value, _ = self.result(body)
        developer = value['input'][0]
        self.assertTrue(developer.get('id', '').startswith('msg_'))
        self.assertEqual(value.get('client_metadata', {}).get('thread_id'), 'lane-example')
        self.assertEqual(value.get('stream_options'), {'reasoning_summary_delivery': 'sequential_cutoff'})
        later, _ = self.result(dict(body, messages=body['messages'] + [{'role':'assistant','content':'Hello'}]))
        self.assertEqual(developer, later['input'][0])
        other = json.loads(request.translate(body, self.model, 'other-lane').body_bytes)
        self.assertNotEqual(developer['id'], other['input'][0]['id'])
        changed, _ = self.result(dict(body, system='Different instructions.'))
        self.assertNotEqual(developer['id'], changed['input'][0]['id'])
        unsupported, _ = self.result({}, support_verbosity=False, reasoning_summary=None)
        self.assertNotIn('text', unsupported)
        self.assertNotIn('stream_options', unsupported)
        unsupported, _ = self.result({'model':'gpt-example-fast'}, service_tiers=())
        self.assertNotIn('service_tier', unsupported)

    def test_empty_tools_still_allows_parallel_calls(self):
        value, _ = self.result({'tools': []}, slug='gpt-6-sol')
        self.assertEqual(value['tools'], [])
        self.assertTrue(value['parallel_tool_calls'])

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
        body['messages'] += [{'role': 'assistant', 'content': 'New reply.'}, {'role': 'user', 'content': 'More.'}]
        later = json.loads(request.translate(body, self.model, 'lane-example', agent_contract=True).body_bytes)
        self.assertEqual(later['input'][:len(value['input'])], value['input'])
        no_system = json.loads(request.translate({'messages': [{'role': 'user', 'content': 'Hi.'}]}, self.model,
                                                 'lane-example', agent_contract=True).body_bytes)
        self.assertNotIn(request.AGENT_CONTRACT, json.dumps(no_system, ensure_ascii=False))

    def test_agent_contract_drops_the_harness_no_report_note_and_defers_to_named_files(self):
        note = ('- Do NOT Write report/summary/findings/analysis .md files. Return findings directly as your final '
                'assistant message \u2014 the parent agent reads your text output, not files you create. (Files written '
                'as input to another tool are fine; this note is about report files.)')
        system = 'Agent manual: save the run to its directory.\n- Keep it short.\n' + note + '\n- Use absolute paths.'
        body = {'system': system, 'messages': [{'role': 'user', 'content': 'Go.'}]}
        agent = json.loads(request.translate(body, self.model, 'lane-example', agent_contract=True).body_bytes)
        text = agent['input'][0]['content'][0]['text']
        self.assertNotIn('Do NOT Write report', text)
        self.assertEqual(text, 'Agent manual: save the run to its directory.\n- Keep it short.\n- Use absolute paths.')
        self.assertIn('names a file', request.AGENT_CONTRACT)
        main = json.loads(request.translate(body, self.model, 'lane-example').body_bytes)
        self.assertIn(note, main['input'][0]['content'][0]['text'])

    def test_prefix_survives_empty_thinking_and_later_tool_history(self):
        body = {'system': [{'type': 'text', 'text': 'Stable instructions.'}], 'messages': [
            {'role': 'user', 'content': 'First question.'}, {'role': 'assistant', 'content': [
                {'type': 'thinking', 'thinking': 'Reasoned once.',
                 'signature': signature.encode('gpt-example', 'rs_1', 'opaque')},
                {'type': 'text', 'text': 'First answer.'}]}, {'role': 'user', 'content': 'Inspect.'}]}
        first, _ = self.result(body)
        self.assertIn('input', first)
        later = copy.deepcopy(body)
        later['messages'][1]['content'][0]['thinking'] = ''
        later['messages'] += [
            {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'call_1', 'name': 'inspect', 'input': {'x': 1}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call_1', 'content': 'Done.'}]}]
        second, _ = self.result(later)
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
        self.assertNotIn('text', value)

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

    def test_every_model_supports_parallel_tools_unless_explicitly_disabled(self):
        for slug in ('gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-astra', 'gpt-6-luna', 'gpt-6-terra', 'gpt-5.5'):
            model = catalog.parse({'models': [{'slug': slug, 'supported_reasoning_levels': []}]})[slug]
            for disabled in (False, True):
                with self.subTest(model=slug, disabled=disabled):
                    body = {'tools': [{'name': 'inspect', 'input_schema': {'type': 'object'}}],
                            'tool_choice': {'type': 'auto', 'disable_parallel_tool_use': disabled}}
                    value = json.loads(request.translate(body, model, 'lane-example').body_bytes)
                    self.assertEqual(value['parallel_tool_calls'], not disabled)
                    self.assertEqual([tool['name'] for tool in value['tools']], ['inspect'])
                    self.assertNotIn('context', value['reasoning'])
                    self.assertTrue(all(item['type'] == 'message' for item in value['input']))

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

    def test_a_malformed_block_costs_only_itself_and_is_noted(self):
        marker = {'type': 'input_text', 'text': '[tool result without its call]'}
        orphan = 'tool result without its call sent as user text'
        for name, body, items, notes in (
                ('tool_use without id or name', {'messages': [{'role': 'assistant', 'content': [{'type': 'tool_use', 'input': {'a': 1}}]}]},
                 [{'type': 'function_call', 'call_id': 'call_unknown_0', 'name': 'unknown', 'arguments': '{"a":1}'},
                  {'type': 'function_call_output', 'call_id': 'call_unknown_0', 'output': 'aborted'}],
                 ('tool_use without id or name sent as call_unknown_0 unknown',)),
                ('tool_result without its call', {'messages': [
                    {'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'c', 'name': 'read', 'input': {}}]},
                    {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'no id'},
                                                 {'type': 'tool_result', 'tool_use_id': 'gone', 'content': [{'type': 'text', 'text': 'lost call'}]},
                                                 {'type': 'tool_result', 'tool_use_id': 'c', 'content': 'read'}]}]},
                 [{'type': 'function_call', 'call_id': 'c', 'name': 'read', 'arguments': '{}'},
                  {'type': 'function_call_output', 'call_id': 'c', 'output': 'read'},
                  {'type': 'message', 'role': 'user', 'content': [marker, {'type': 'input_text', 'text': 'no id'}]},
                  {'type': 'message', 'role': 'user', 'content': [marker, {'type': 'input_text', 'text': 'lost call'}]}],
                 (orphan, orphan)),
                ('text without text', {'system': [{'type': 'text'}], 'messages': [{'role': 'user', 'content': [{'type': 'text'}]}]},
                 [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': ''}]}],
                 ('text block without text sent empty', 'text block without text sent empty')),
                ('base64 image without data', {'messages': [{'role': 'user', 'content': [
                    {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png'}}]}]},
                 [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': '[unsupported image]'}]}], ()),
                ('json_schema without schema', {'output_config': {'format': {'type': 'json_schema'}},
                                                'messages': [{'role': 'user', 'content': 'hi'}]},
                 [{'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'hi'}]}],
                 ('json_schema output format without a schema dropped',))):
            with self.subTest(name=name):
                value, translated = self.result(body)
                self.assertEqual((value['input'], value.get('text'), translated.notes), (items, {'verbosity': 'low'}, notes))

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

    def test_deferred_tools_join_top_level_tools_and_results_keep_references(self):
        value, translated = self.result(self.deferred_body(), slug='gpt-6-sol')
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', 'ToolSearch', 'WebFetch', 'Monitor'])
        self.assertEqual([x['type'] for x in value['input']],
                         ['message', 'message', 'function_call', 'function_call_output', 'message'])
        self.assertEqual(value['input'][3]['output'], 'Tool loaded: WebFetch')
        self.assertEqual(value['input'][4]['content'], [{'type': 'input_text', 'text': 'Tool loaded.'}])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())

    def test_history_prefix_stays_the_same_before_and_after_discovery(self):
        before = self.deferred_body(loaded=False)
        before['messages'] = before['messages'][:1]
        first, _ = self.result(before, slug='gpt-6-sol')
        later, _ = self.result(self.deferred_body(referenced=('WebFetch', 'Monitor')), slug='gpt-6-sol')
        self.assertEqual(first['input'], later['input'][:2])
        self.assertEqual(later['input'][3]['output'], 'Tool loaded: WebFetch\nTool loaded: Monitor')

    def test_repeated_unlisted_and_inline_references_do_not_duplicate_tools(self):
        body = self.deferred_body(referenced=('WebFetch', 'Bash', 'Gone'))
        body['messages'] += [{'role': 'assistant', 'content': [{'type': 'tool_use', 'id': 'toolu_2', 'name': 'ToolSearch',
                                                                'input': {}}]},
                             {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'toolu_2', 'content': [
                                 {'type': 'tool_reference', 'tool_name': 'WebFetch'}]}]}]
        value, _ = self.result(body, slug='gpt-6-sol')
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', 'ToolSearch', 'WebFetch', 'Monitor'])
        outputs = [x['output'] for x in value['input'] if x['type'] == 'function_call_output']
        self.assertEqual(outputs, ['Tool loaded: WebFetch\nTool loaded: Bash\nTool loaded: Gone', 'Tool loaded: WebFetch'])

    def test_deferred_tool_names_stay_reversible_in_top_level_tools(self):
        long = 'mcp__' + 'y' * 85
        extra = [{'name': long, 'description': 'Long.', 'input_schema': {'type': 'object'}, 'defer_loading': True}]
        body = self.deferred_body(referenced=(long,), tools_extra=extra)
        _, translated = self.result(body, slug='gpt-6-sol')
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

    def test_tool_additions_join_top_level_tools_and_preserve_system_text(self):
        schema = {'type': 'object', 'properties': {'id': {'type': 'string'}}, 'required': ['id']}
        body = self.addition_body(('docs_batch', schema), 'docs_guide')
        value, translated = self.result(body, slug='gpt-6-sol')
        self.assertEqual([x['type'] for x in value['input']], ['message'] * 4)
        self.assertEqual(value['input'][2]['content'], [{'type': 'input_text', 'text': 'Docs connected.'}])
        self.assertEqual(value['tools'][1],
                         {'type': 'function', 'name': 'docs_batch', 'description': 'Does docs_batch.', 'strict': False,
                          'parameters': schema})
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', 'docs_batch', 'docs_guide'])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())
        before = dict(body, messages=body['messages'][:1])
        first, _ = self.result(before, slug='gpt-6-sol')
        self.assertEqual(first['input'], value['input'][:2])

    def test_tool_additions_without_system_text_leave_no_message(self):
        body = self.addition_body('docs_batch', 'docs_guide')
        body['messages'][1]['content'] = body['messages'][1]['content'][1:]
        value, translated = self.result(body)
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', 'docs_batch', 'docs_guide'])
        self.assertEqual([x['type'] for x in value['input']], ['message', 'message', 'message'])
        self.assertNotIn('[unsupported', translated.body_bytes.decode())

    def test_a_tool_addition_is_delivered_once_stays_reversible_and_collides_loudly(self):
        long = 'mcp__' + 'z' * 85
        body = self.addition_body(long, 'Bash', 'docs_batch')
        body['messages'].append(dict(body['messages'][1]))
        value, translated = self.result(body, slug='gpt-6-sol')
        self.assertEqual([t['name'] for t in value['tools']], ['Bash', tools.short_name(long), 'docs_batch'])
        self.assertEqual(translated.tool_names, {tools.short_name(long): long})
        clash = self.addition_body(long)
        clash['tools'].append({'name': tools.short_name(long), 'input_schema': {'type': 'object'}})
        with self.assertRaises(ValueError):
            self.result(clash, slug='gpt-6-sol')

    def test_a_malformed_tool_addition_stays_a_placeholder(self):
        for block in [{'type': 'tool_addition'}, {'type': 'tool_addition', 'tool': {'type': 'tool_definition',
                                                                                    'definition': {'name': ''}}}]:
            with self.subTest(block=block):
                body = {'messages': [{'role': 'system', 'content': [block]}]}
                value, _ = self.result(body, slug='gpt-6-sol')
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
