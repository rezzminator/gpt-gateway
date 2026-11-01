"""Deterministic Anthropic Messages to the Codex Responses wire body.

Derived from openai/codex, changed (Apache-2.0). Message and tool-result mapping
is distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT).
Permission notices: THIRD_PARTY_NOTICES.md.
"""
from dataclasses import dataclass
import json
import re
import uuid
from typing import Optional

from ..hooks import last_turn_message
from . import signature, tools
from .catalog import Model

_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_IMAGE_MEDIA = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
# Claude Code's activity-summary fork: a 3-5 word label of the agent's last action, asked as the last user text.
_SUMMARY_ASK = 'Describe your most recent action in 3-5 words'
# Claude Code's compaction request: every summary prompt it sends opens with this preamble, asked as the last user text.
_COMPACTION_ASK = 'CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.'
# Claude Code's sub-agent delivery, which GPT models do not infer from its system text: appended to that developer item.
AGENT_CONTRACT = (
    'When another agent spawned you with a task: your final message is your return. Claude Code hands it to that agent '
    'whole, so put the entire result there. Text you write before it stays in your transcript, and no one reads it '
    'while you work. SendMessage wakes its recipient and costs it a full turn: send one only when your instructions '
    'explicitly require it, or when you are blocked on a decision only the recipient can make. Never send one for '
    'progress, interim findings, corrections, or a preview of your final message. An agent you spawn answers you '
    'through its own final message. Write a report file only when your instructions name one: a save step, a run '
    'directory or a report path names a file, and you write it.')

# Claude Code's sub-agent note against report files. A GPT model obeys it over the agent's own save step, so a
# sub-agent's system text drops it and AGENT_CONTRACT carries the rule that defers to a file the instructions name.
_HARNESS_NO_REPORT_NOTE = re.compile(r'^- Do NOT Write report/summary/findings/analysis \.md files\..*(?:\n|$)', re.M)


@dataclass(frozen=True)
class Translation:
    body_bytes: bytes
    tool_names: dict
    notes: tuple
    estimate_chars: int
    effort: Optional[str] = None  # the reasoning effort the body sends, None when it names none
    supports_effort_updates: bool = False


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


# codex-rs/codex-api/src/common.rs:278-304; core/src/client.rs:947-1071.
# claude-code-proxy/src/providers/codex/translate/request.rs:480-549.
def translate(body: dict, model: Model, lane_key: str, effort: Optional[str] = None,
              agent_contract: bool = False, retention: Optional[str] = None,
              parent_key: Optional[str] = None) -> Translation:
    """Translate without I/O or mutation; estimate_chars counts serialized Unicode characters, image and file data excluded.

    effort, when given, replaces the effort the body asks for: the route's pin of an activity-summary fork or a compaction
    request to its lane. agent_contract appends AGENT_CONTRACT to the system text's developer item, a second part so
    the system text itself, and the model-switch text quoting it, stay byte-identical. retention, when given
    ("24h"), rides as prompt_cache_retention right after the cache key; nothing else in the body changes."""
    registered, deferred, names, tool_notes = tools.translate_tools(body)
    notes = list(tool_notes)
    instructions = _system_text(body.get('system', []), notes)
    if agent_contract:
        stripped, count = _HARNESS_NO_REPORT_NOTE.subn('', instructions)
        instructions = stripped.rstrip('\n') if count else instructions
    known = {tool['name'] for tool in registered + list(deferred.values()) if tool['type'] == 'function'}
    added = tools.added_tools(body, known, names)
    items = _request_items(body.get('messages', []), model.slug, instructions, notes)
    registered = registered + list(deferred.values()) + list(added.values())
    if instructions:
        parts = [{'type': 'input_text', 'text': instructions}]
        if agent_contract:
            parts.append({'type': 'input_text', 'text': AGENT_CONTRACT})
        developer = _response_message('developer', parts)
        namespace = uuid.uuid5(uuid.NAMESPACE_OID, lane_key)
        developer['id'] = 'msg_' + str(uuid.uuid5(namespace, '\n\n'.join(part['text'] for part in parts)))
        items.insert(0, developer)
    reasoning = {}
    effort = _request_effort(body, model, notes, effort)
    if effort is not None:
        reasoning['effort'] = effort
    if model.reasoning_summary is not None:  # codex-rs/core/src/client.rs:880-882
        reasoning['summary'] = model.reasoning_summary
    choice = body.get('tool_choice', {})
    disable_parallel = choice.get('disable_parallel_tool_use', False) if isinstance(choice, dict) else False
    result = {'model': model.slug, 'stream': True}
    raw_model = body.get('model', model.slug)
    if raw_model.endswith('[1m]'):
        raw_model = raw_model[:-4]
    tier = model.service_tier_for_request('fast' if raw_model.endswith('-fast') else None)
    if tier is not None:
        result['service_tier'] = tier
    result['input'] = _normalize_calls(items, notes)
    result['tools'] = registered
    result['tool_choice'] = tools.tool_choice(body, names)
    result['parallel_tool_calls'] = not disable_parallel
    result['reasoning'] = reasoning
    result['store'] = False
    result['include'] = ['reasoning.encrypted_content']
    if model.reasoning_summary is not None:
        result['stream_options'] = {'reasoning_summary_delivery': 'sequential_cutoff'}
    result['prompt_cache_key'] = lane_key
    if retention is not None:
        result['prompt_cache_retention'] = retention
    text = {}
    if model.support_verbosity and model.default_verbosity is not None:
        text['verbosity'] = model.default_verbosity
    output_format = body.get('output_config', {}).get('format')
    if isinstance(output_format, dict) and output_format.get('type') == 'json_schema':
        if 'schema' in output_format:
            text['format'] = {'type': 'json_schema', 'name': output_format.get('name', 'response'),
                              'strict': True, 'schema': output_format['schema']}
        else:
            notes.append('json_schema output format without a schema dropped')
    if text:
        result['text'] = text
    metadata = {'session_id': lane_key, 'thread_id': lane_key, 'x-codex-window-id': parent_key or lane_key}
    turn_metadata = {'session_id': lane_key, 'thread_id': lane_key, 'window_id': parent_key or lane_key,
                     'request_kind': 'compaction' if compaction(body) else 'turn'}
    if parent_key:
        metadata.update({'x-codex-parent-thread-id': parent_key, 'x-openai-subagent': 'collab_spawn'})
        turn_metadata['parent_thread_id'] = parent_key
    metadata['x-codex-turn-metadata'] = json.dumps(turn_metadata, separators=(',', ':'), ensure_ascii=True)
    result['client_metadata'] = metadata
    serialized = _compact_json(result)
    return Translation(serialized.encode('utf-8'), names, tuple(notes),
                       len(serialized) - _attachment_chars(result['input']), effort, model.supports_reasoning_effort_updates)


def activity_summary(body: dict) -> bool:
    """True when the last turn message is the user's, and its own text, never a tool result's, opens with the summary ask."""
    return _last_user_text_opens(body, _SUMMARY_ASK)


def compaction(body: dict) -> bool:
    """True when the last turn message is the user's, and its own text, never a tool result's, opens with the compaction ask."""
    return _last_user_text_opens(body, _COMPACTION_ASK)


def _last_user_text_opens(body, opening):
    last = last_turn_message(body)
    if not isinstance(last, dict) or last.get('role') != 'user':
        return False
    content = last.get('content')
    blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
    return isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
        and block['text'].startswith(opening) for block in blocks)


# claude-code-proxy/src/providers/kimi/count_tokens.rs:7: an image is a flat allowance, never its payload;
# a PDF takes the same allowance.
_PAYLOAD_KEYS = {'input_image': 'image_url', 'input_file': 'file_data'}


def _attachment_chars(items):
    parts = []
    for item in items:
        value = item.get('content') if item['type'] == 'message' else item.get('output')
        parts.extend(value if isinstance(value, list) else [])
    return sum(len(part[_PAYLOAD_KEYS[part['type']]]) for part in parts if part.get('type') in _PAYLOAD_KEYS)


# claude-code-proxy/src/providers/translate_shared.rs:36-58.
def _system_text(content, notes):
    texts = [_block_text(block, notes) for block in _content_blocks(content) if block.get('type') == 'text']
    return '\n\n'.join(text for text in texts if not text.startswith('x-anthropic-billing-header:'))


def _block_text(block, notes):
    """A text block's text; one without a string text is sent empty and noted."""
    text = block.get('text')
    if isinstance(text, str):
        return text
    notes.append('text block without text sent empty')
    return ''


def _content_blocks(content):
    return [{'type': 'text', 'text': content}] if isinstance(content, str) else content


# claude-code-proxy/src/providers/codex/translate/request.rs:865-980.
def _response_message(role, parts):
    return {'type': 'message', 'role': role, 'content': parts}


# claude-code-proxy/src/providers/codex/translate/request.rs:865-980.
# codex-rs/core/src/context/model_switch_instructions.rs:17-40.
def _request_items(messages, slug, instructions, notes):
    """Lower history; tool definitions live in the request's top-level tools."""
    items = []
    switch_after = None
    for message in messages:
        role = 'developer' if message['role'] == 'system' else message['role']
        parts = []
        switched = False
        for block in _content_blocks(message.get('content', [])):
            kind = block.get('type', 'unknown')
            item = None
            if message['role'] == 'system' and tools.added_name(block) is not None:
                continue
            if role == 'assistant' and kind == 'thinking':
                try:
                    decoded = signature.decode(block.get('signature'))
                except ValueError:
                    notes.append('damaged reasoning signature dropped')
                    decoded = None
                if decoded is not None:
                    original_slug, item_id, encrypted = decoded
                    if original_slug == slug:
                        item = {'type': 'reasoning', 'id': item_id, 'summary': [], 'encrypted_content': encrypted}
                    else:
                        switched = True
                if item is None:
                    continue
            elif role == 'assistant' and kind == 'tool_use':
                call_id, name = block.get('id'), block.get('name')
                if not (isinstance(call_id, str) and isinstance(name, str)):
                    call_id = call_id if isinstance(call_id, str) else 'call_unknown_%d' % len(items)
                    name = name if isinstance(name, str) else 'unknown'
                    notes.append('tool_use without id or name sent as %s %s' % (call_id, name))
                item = {'type': 'function_call', 'call_id': call_id, 'name': tools.short_name(name),
                        'arguments': _compact_json(block.get('input', {}))}
            elif role == 'user' and kind == 'tool_result':  # one without tool_use_id goes as user text
                item = {'type': 'function_call_output', 'call_id': block.get('tool_use_id'),
                        'output': _tool_output(block, notes)}
            if item is not None:
                if parts:
                    items.append(_response_message(role, parts))
                    parts = []
                items.append(item)
            else:
                parts.append(_content_part(block, notes, role))
        if parts:
            items.append(_response_message(role, parts))
        if switched:
            switch_after = len(items)
    if switch_after is not None:
        # Anchor at the final old-model turn; appending new turns cannot move this marker.
        marker = ('<model_switch>\nThe user was previously using a different model. '
                  'Please continue the conversation according to the following instructions:\n\n'
                  + instructions + '\n</model_switch>')
        items.insert(switch_after, _response_message('developer', [{'type': 'input_text', 'text': marker}]))
    return items


# claude-code-proxy/src/providers/codex/translate/request.rs:865-908;
# codex-rs/utils/image/src/lib.rs:181-207 (remote-image placeholder policy).
def _content_part(block, notes, role='user'):
    kind = block.get('type', 'unknown')
    text_type = 'output_text' if role == 'assistant' else 'input_text'
    if kind == 'text':
        return {'type': text_type, 'text': _block_text(block, notes)}
    source = block.get('source')
    base64 = isinstance(source, dict) and source.get('type') == 'base64' and isinstance(source.get('data'), str)
    if kind == 'image' and role != 'assistant':
        if base64 and source.get('media_type') in _IMAGE_MEDIA:
            return {'type': 'input_image', 'image_url': 'data:' + source['media_type'] + ';base64,' + source['data']}
    # Responses API input_file: inline file_data is a data URL and needs a filename.
    if kind == 'document' and role != 'assistant':
        if base64 and source.get('media_type') == 'application/pdf':
            return {'type': 'input_file', 'filename': 'document.pdf',
                    'file_data': 'data:application/pdf;base64,' + source['data']}
    if kind == 'tool_reference' and isinstance(block.get('tool_name'), str):
        return {'type': text_type, 'text': 'Tool loaded: ' + block['tool_name']}
    return {'type': text_type, 'text': '[unsupported ' + kind + ']'}


# claude-code-proxy/src/providers/codex/translate/request.rs:877-908.
def _tool_output(block, notes):
    parts = [_content_part(part, notes) for part in _content_blocks(block.get('content', []))]
    if block.get('is_error'):
        parts.insert(0, {'type': 'input_text', 'text': '[tool execution error]'})
    if any(part['type'] in ('input_image', 'input_file') for part in parts):
        return parts
    return '\n'.join(part['text'] for part in parts)


# codex-rs/core/src/context_manager/normalize.rs:46-67 (synthetic output); orphan output policy: an output without
# its call goes as user text after the run of outputs it sat in, so no message splits a call from its output.
def _normalize_calls(items, notes):
    calls = {item['call_id'] for item in items if item['type'] == 'function_call'}
    outputs = {item['call_id'] for item in items if item['type'] == 'function_call_output'}
    normalized, held = [], []
    for item in items:
        if item['type'] == 'function_call_output' and item['call_id'] not in calls:
            output = item['output']
            held.append(_response_message('user', [{'type': 'input_text', 'text': '[tool result without its call]'}] + (
                output if isinstance(output, list) else [{'type': 'input_text', 'text': output}])))
            notes.append('tool result without its call sent as user text')
            continue
        if item['type'] != 'function_call_output':
            normalized.extend(held)
            held = []
        normalized.append(item)
        if item['type'] == 'function_call' and item['call_id'] not in outputs:
            normalized.append({'type': 'function_call_output', 'call_id': item['call_id'], 'output': 'aborted'})
    return normalized + held


def _request_effort(body, model, notes, pinned=None):
    """Apply the design's budget thresholds, then choose a catalog-supported effort; a pinned effort replaces the asked one."""
    requested = pinned if pinned is not None else body.get('output_config', {}).get('effort')
    if requested is None:
        budget = body.get('thinking', {}).get('budget_tokens')
        requested = model.default_reasoning if budget is None else (
            'high' if budget >= 4096 else 'medium' if budget >= 2048 else 'low' if budget >= 1024 else 'minimal')
    if requested is None:
        return None
    if requested in model.reasoning_levels:
        return requested
    supported = [effort for effort in _EFFORTS if effort in model.reasoning_levels]
    if not supported:
        notes.append('no supported reasoning effort for requested value')
        return None
    if requested not in _EFFORTS:
        notes.append('unknown reasoning effort: ' + str(requested))
        return model.default_reasoning if model.default_reasoning in model.reasoning_levels else supported[-1]
    lower = [effort for effort in supported if _EFFORTS.index(effort) <= _EFFORTS.index(requested)]
    return lower[-1] if lower else supported[0]
