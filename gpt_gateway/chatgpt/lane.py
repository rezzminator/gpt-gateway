"""Deterministic cache affinity from an unambiguous session/agent pair."""
import json
import uuid


def key(session_id, agent_id):
    """One UUID per session/agent pair; the header and prompt_cache_key share it."""
    pair = json.dumps([session_id, agent_id], separators=(',', ':'))
    return str(uuid.uuid5(uuid.NAMESPACE_OID, pair))
