"""Function registration, hosted search, and forced choices for Responses.

Distilled from claude-code-proxy, Copyright (c) 2026 Raine Virta (MIT).
Permission notice: THIRD_PARTY_NOTICES.md. Name hashing is an original
implementation of the design's 64-character prefix-and-hash rule.
"""
import hashlib

from . import schema


def short_name(name):
    """An order-independent name, shared by registration, calls and choice."""
    if len(name) <= 64:
        return name
    return name[:47] + '_' + hashlib.sha256(name.encode('utf-8')).hexdigest()[:16]


# Claude Code's tool search leaves this one deferred tool in every request to keep the list non-empty; no model calls it.
_PLACEHOLDER = 'DeferredToolPlaceholder'


# claude-code-proxy/src/providers/codex/translate/request.rs:637-711 (changed server-tool policy).
def translate_tools(body):
    """(tools, deferred tools by original name, short-to-original names, notes), retaining the client's order.

    A tool marked defer_loading is returned separately in deferred; the request translator merges it into the
    top-level tools list regardless of tool_reference history. Its short name is in the name map like any other."""
    converted, deferred, names, notes = [], {}, {}, []
    registered = set()
    for tool in body.get('tools', []):
        kind = tool.get('type', 'function')
        if kind == 'web_search_20250305':
            hosted = {'type': 'web_search', 'external_web_access': True}
            filters = {key: tool[key] for key in ('allowed_domains', 'blocked_domains') if tool.get(key)}
            if filters:
                hosted['filters'] = filters
            converted.append(hosted)
        elif kind in ('function', 'custom'):
            original = tool['name']
            if original == _PLACEHOLDER:
                continue
            name = short_name(original)
            if name in registered:
                raise ValueError('duplicate or colliding tool name: ' + name)
            registered.add(name)
            if original != name:
                names[name] = original
            function = _function(name, tool)
            if tool.get('defer_loading') is True:
                deferred[original] = function
            else:
                converted.append(function)
        else:
            notes.append('dropped server tool: ' + str(kind))
    return converted, deferred, names, tuple(notes)


def _function(name, tool):
    return {'type': 'function', 'name': name, 'description': tool.get('description', ''),
            'strict': False, 'parameters': schema.sanitize(tool.get('input_schema', {}))}


def added_name(block):
    """The original tool name a well-formed tool_addition block adds, else None."""
    tool = block.get('tool') if block.get('type') == 'tool_addition' else None
    definition = tool.get('definition') if isinstance(tool, dict) and tool.get('type') == 'tool_definition' else None
    name = definition.get('name') if isinstance(definition, dict) else None
    return name if isinstance(name, str) and name else None


def added_tools(body, taken, names):
    """Tools Claude Code added mid-conversation through role-system tool_addition blocks: lowered function by original
    name, in order, each once. taken: the short names already registered or deferred, which add nothing again; a different
    original on a taken short name raises ValueError like any collision. names gains each shortened name."""
    added, taken = {}, set(taken)
    for message in body.get('messages', []):
        content = message.get('content') if isinstance(message, dict) and message.get('role') == 'system' else None
        for block in content if isinstance(content, list) else []:
            original = added_name(block) if isinstance(block, dict) else None
            if original is None or original in added or original == _PLACEHOLDER:
                continue
            name = short_name(original)
            if name in taken:
                if names.get(name, name) != original:
                    raise ValueError('duplicate or colliding tool name: ' + name)
                continue
            taken.add(name)
            if original != name:
                names[name] = original
            added[original] = _function(name, block['tool']['definition'])
    return added


# claude-code-proxy/src/providers/codex/translate/request.rs:803-862.
def tool_choice(body, names):
    choice = body.get('tool_choice', 'auto')
    kind = choice.get('type', 'auto') if isinstance(choice, dict) else choice
    if kind in ('auto', 'none'):
        return kind
    if kind in ('any', 'required'):
        return 'required'
    if kind == 'tool':
        name = choice.get('name', '')
        registered = body.get('tools', [])
        search = any(t.get('type') == 'web_search_20250305' and t.get('name') == name for t in registered)
        if search:
            return {'type': 'allowed_tools', 'mode': 'required', 'tools': [{'type': 'web_search'}]}
        if name == 'web_search' and not any(t.get('name') == name for t in registered):
            return 'auto'
        if any(t.get('name') == name and t.get('type', 'function') not in ('function', 'custom') for t in registered):
            return 'auto'
        reverse = {original: shortened for shortened, original in names.items()}
        return {'type': 'function', 'name': reverse.get(name, short_name(name))}
    return 'auto'
