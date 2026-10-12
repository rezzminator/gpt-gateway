"""Deterministic per-agent cache affinity, using the design's UUIDv5 lane formula."""
import uuid


def key(session_id, agent_id):
    """One UUID per session/agent pair; the header and prompt_cache_key share it."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f'{session_id}:{agent_id}'))
