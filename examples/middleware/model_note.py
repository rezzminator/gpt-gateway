"""model-note: the smallest model-gateway middleware, and the contract's executable documentation.

Copy it to `{middleware_dir}/model_note.py` and list it in settings.json:

    {"middleware": [{"module": "model_note", "settings": {"enabled": true}}]}

Each proxied request's log line then ends with ` model-note=<the request's model>`. It edits no
body, so every request goes upstream byte for byte. `MODEL_GATEWAY_MODEL_NOTE=0` turns it off
without touching the file; an env value reaches the validator as a string.
"""

NAME = "model-note"


def enabled_value(value):
    """A bool from the file (true/false) or from the env (1/0, true/false, yes/no, on/off)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("1", "true", "yes", "on"):
            return True
        if word in ("0", "false", "no", "off"):
            return False
    raise ValueError("enabled must be true or false (env: 1/0, true/false, yes/no, on/off), got %r" % (value,))


SETTINGS = {"enabled": (True, enabled_value, "MODEL_GATEWAY_MODEL_NOTE")}


def on_request(ctx, req):
    # ctx.route names the selected upstream: "anthropic" or "chatgpt".
    if ctx.settings["enabled"]:
        return " model-note=%s" % ctx.model
    return None
