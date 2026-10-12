"""The reasoning signature: GPT's encrypted reasoning carried inside an Anthropic thinking block.

Claude Code sends a thinking block's `signature` back untouched on every later turn, so it is the one
channel a GPT reasoning item can round-trip through. The signature names the slug that minted it, so a
model switch inside a lane can drop it, and the item id, so the replayed item is the one GPT emitted.
"""
import base64

PREFIX = "gpt-gateway:chatgpt:v1:"


def _b64(text):
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.b64decode((text + "=" * (-len(text) % 4)).encode("ascii"), altchars=b"-_", validate=True).decode("utf-8")


def encode(slug, item_id, encrypted_content):
    """The signature for one reasoning item; every argument a non-empty str."""
    for name, value in (("slug", slug), ("item_id", item_id), ("encrypted_content", encrypted_content)):
        if not isinstance(value, str) or not value:
            raise ValueError("signature %s must be a non-empty string, got %r" % (name, value))
    return PREFIX + _b64(slug) + ":" + _b64(item_id) + ":" + encrypted_content


def decode(signature):
    """(slug, item_id, encrypted_content) for a signature this module minted, None for any other.

    A signature that carries the prefix but does not parse raises ValueError naming why: it was ours and
    is damaged, which the caller logs, never reads as foreign.
    """
    if not isinstance(signature, str) or not signature.startswith(PREFIX):
        return None
    parts = signature[len(PREFIX):].split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError("damaged %s signature: expected slug:item_id:content" % PREFIX.rstrip(":"))
    try:
        slug, item_id = _unb64(parts[0]), _unb64(parts[1])
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("damaged %s signature: %s" % (PREFIX.rstrip(":"), e))
    return slug, item_id, parts[2]
