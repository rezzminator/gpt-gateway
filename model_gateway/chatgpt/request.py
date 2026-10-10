"""Deterministic Anthropic Messages to the Codex Responses wire body.

Derived from openai/codex, changed (Apache-2.0). Message and tool-result mapping
is distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT).
Permission notices: THIRD_PARTY_NOTICES.md.
"""
from dataclasses import dataclass
import json
from typing import Optional

from . import signature, tools
from .catalog import Model

_EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
_IMAGE_MEDIA = ('image/png', 'image/jpeg', 'image/gif', 'image/webp')
# Claude Code's activity-summary fork: a 3-5 word label of the agent's last action, asked as the last user text.
_SUMMARY_ASK = 'Describe your most recent action in 3-5 words'


@dataclass(frozen=True)
class Translation:
    body_bytes: bytes
    lite: bool
    tool_names: dict
    notes: tuple
    estimate_chars: int
    effort: Optional[str] = None  # the reasoning effort the body sends, None when it names none


def _compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


# codex-rs/codex-api/src/common.rs:278-304; core/src/client.rs:947-1071.
# claude-code-proxy/src/providers/codex/translate/request.rs:480-549.
def translate(body: dict, model: Model, lane_key: str, effort: Optional[str] = None) -> Translation:
    """Translate without I/O or mutation; estimate_chars counts serialized Unicode characters, image and file data excluded.

    effort, when given, replaces the effort the body asks for: the route's pin of an activity-summary fork to its lane."""
    registered, names, tool_notes = tools.translate_tools(body)
    notes = list(tool_notes)
    instructions = _system_text(body.get('system', []))
    items = _request_items(body.get('messages', []), model.slug, instructions, notes)
    if instructions:
        items.insert(0, _response_message('developer', [{'type': 'input_text', 'text': instructions}]))
    if model.responses_lite and registered:
        items.insert(0, {'type': 'additional_tools', 'role': 'developer', 'tools': registered})
    reasoning = {}
    effort = _request_effort(body, model, notes, effort)
    if effort is not None:
        reasoning['effort'] = effort
    if model.reasoning_summary is not None:  # codex-rs/core/src/client.rs:880-882
        reasoning['summary'] = model.reasoning_summary
    if model.responses_lite:
        reasoning['context'] = 'all_turns'
    choice = body.get('tool_choice', {})
    disable_parallel = choice.get('disable_parallel_tool_use', False) if isinstance(choice, dict) else False
    result = {'model': model.slug, 'stream': True}
    raw_model = body.get('model', model.slug)
    if raw_model.endswith('[1m]'):
        raw_model = raw_model[:-4]
    if raw_model.endswith('-fast'):
        result['service_tier'] = 'priority'
    result['input'] = _normalize_calls(items)
    if not model.responses_lite:
        result['tools'] = registered
    result['tool_choice'] = tools.tool_choice(body, names)
    result['parallel_tool_calls'] = not (disable_parallel or model.responses_lite)
    result['reasoning'] = reasoning
    result['store'] = False
    result['include'] = ['reasoning.encrypted_content']
    result['prompt_cache_key'] = lane_key
    text = {}
    if model.default_verbosity is not None:
        text['verbosity'] = model.default_verbosity
    output_format = body.get('output_config', {}).get('format')
    if isinstance(output_format, dict) and output_format.get('type') == 'json_schema':
        text['format'] = {'type': 'json_schema', 'name': output_format.get('name', 'response'),
                          'strict': True, 'schema': output_format['schema']}
    result['text'] = text
    serialized = _compact_json(result)
    return Translation(serialized.encode('utf-8'), model.responses_lite, names, tuple(notes),
                       len(serialized) - _attachment_chars(result['input']), effort)


def last_turn_message(body: dict):
    """The last user or assistant message, past the harness notices Claude Code appends as trailing system messages."""
    messages = body.get('messages')
    for message in reversed(messages if isinstance(messages, list) else []):
        if not isinstance(message, dict) or message.get('role') != 'system':
            return message
    return None


def activity_summary(body: dict) -> bool:
    """True when the last turn message is the user's, and its own text, never a tool result's, opens with the summary ask."""
    last = last_turn_message(body)
    if not isinstance(last, dict) or last.get('role') != 'user':
        return False
    content = last.get('content')
    blocks = [{'type': 'text', 'text': content}] if isinstance(content, str) else content
    return isinstance(blocks, list) and any(
        isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str)
        and block['text'].startswith(_SUMMARY_ASK) for block in blocks)


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
def _system_text(content):
    blocks = _content_blocks(content)
    return '\n\n'.join(block['text'] for block in blocks if block.get('type') == 'text'
                       and not block['text'].startswith('x-anthropic-billing-header:'))


def _content_blocks(content):
    return [{'type': 'text', 'text': content}] if isinstance(content, str) else content


# claude-code-proxy/src/providers/codex/translate/request.rs:865-980.
def _response_message(role, parts):
    return {'type': 'message', 'role': role, 'content': parts}


# claude-code-proxy/src/providers/codex/translate/request.rs:865-980.
# codex-rs/core/src/context/model_switch_instructions.rs:17-40.
def _request_items(messages, slug, instructions, notes):
    items = []
    switch_after = None
    for message in messages:
        role = 'developer' if message['role'] == 'system' else message['role']
        parts = []
        switched = False
        for block in _content_blocks(message.get('content', [])):
            kind = block.get('type', 'unknown')
            item = None
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
                item = {'type': 'function_call', 'call_id': block['id'], 'name': tools.short_name(block['name']),
                        'arguments': _compact_json(block.get('input', {}))}
            elif role == 'user' and kind == 'tool_result':
                item = {'type': 'function_call_output', 'call_id': block['tool_use_id'],
                        'output': _tool_output(block)}
            if item is not None:
                if parts:
                    items.append(_response_message(role, parts))
                    parts = []
                items.append(item)
            else:
                parts.append(_content_part(block, role))
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
def _content_part(block, role='user'):
    kind = block.get('type', 'unknown')
    text_type = 'output_text' if role == 'assistant' else 'input_text'
    if kind == 'text':
        return {'type': text_type, 'text': block['text']}
    if kind == 'image' and role != 'assistant':
        source = block.get('source', {})
        if source.get('type') == 'base64' and source.get('media_type') in _IMAGE_MEDIA:
            return {'type': 'input_image', 'image_url': 'data:' + source['media_type'] + ';base64,' + source['data']}
    # Responses API input_file: inline file_data is a data URL and needs a filename.
    if kind == 'document' and role != 'assistant':
        source = block.get('source', {})
        if source.get('type') == 'base64' and source.get('media_type') == 'application/pdf':
            return {'type': 'input_file', 'filename': 'document.pdf',
                    'file_data': 'data:application/pdf;base64,' + source['data']}
    return {'type': text_type, 'text': '[unsupported ' + kind + ']'}


# claude-code-proxy/src/providers/codex/translate/request.rs:877-908.
def _tool_output(block):
    parts = [_content_part(part) for part in _content_blocks(block.get('content', []))]
    if block.get('is_error'):
        parts.insert(0, {'type': 'input_text', 'text': '[tool execution error]'})
    if any(part['type'] in ('input_image', 'input_file') for part in parts):
        return parts
    return '\n'.join(part['text'] for part in parts)


# codex-rs/core/src/context_manager/normalize.rs:46-67 (synthetic output); orphan output policy.
def _normalize_calls(items):
    calls = {item['call_id'] for item in items if item['type'] == 'function_call'}
    outputs = {item['call_id'] for item in items if item['type'] == 'function_call_output'}
    normalized = []
    for item in items:
        if item['type'] == 'function_call_output' and item['call_id'] not in calls:
            continue
        normalized.append(item)
        if item['type'] == 'function_call' and item['call_id'] not in outputs:
            normalized.append({'type': 'function_call_output', 'call_id': item['call_id'], 'output': 'aborted'})
    return normalized


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
