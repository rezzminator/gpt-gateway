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

    A tool marked defer_loading stays out of tools and is returned in deferred: it reaches the model only after a
    tool_reference names it. Its short name is in the name map like any other."""
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
            function = {'type': 'function', 'name': name, 'description': tool.get('description', ''),
                        'strict': False, 'parameters': schema.sanitize(tool.get('input_schema', {}))}
            if tool.get('defer_loading') is True:
                deferred[original] = function
            else:
                converted.append(function)
        else:
            notes.append('dropped server tool: ' + str(kind))
    return converted, deferred, names, tuple(notes)


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
