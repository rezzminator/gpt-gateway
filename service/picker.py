"""Install-time Claude Code model picker reconciliation, without network or service access."""
import json
import os
import shutil
import stat
import sys
import tempfile

# Source checkout: service/ beside the package. Installed copy: flat beside the package.
_directory = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _directory if os.path.isdir(os.path.join(_directory, "gpt_gateway"))
                else os.path.dirname(_directory))

from gpt_gateway import env, settings
from gpt_gateway.chatgpt import catalog

DESCRIPTION = "GPT via local gpt-gateway (ChatGPT plan)"


def warn(text):
    print("gpt-gateway: model picker: warning: %s" % text, file=sys.stderr)


def picker_rows(install_dir, environ):
    """Validate configuration and local availability even when the route is off; never fetch a catalog.

    The catalog cache only labels rows: one that cannot be read gives the default labels, an entry or label that does
    not parse costs only itself, each with a warning. A model the cache lacks is refused when the cache was read whole;
    when it could not be read, or skipped an entry, the model stays with its default label and a warning."""
    path = settings.default_path(install_dir)
    values = settings.read_file(path) or {}
    override = env.model_picker_override(environ)
    where = "env " + env.MODEL_PICKER_MODELS if override is not None else path + ": model_picker_models"
    models = settings.model_picker_models(
        override if override is not None else values.get("model_picker_models", list(settings.DEFAULT_PICKER_MODELS)),
        where)
    labels, whole = {}, False
    if models:
        cache = env.chatgpt_path(environ, env.CHATGPT_MODELS_PATH, "~/.local/state/gpt-gateway/chatgpt-models.json")
        raw, faults, parsed = None, [], None
        try:
            with open(cache, encoding="utf-8") as source:
                raw = json.load(source)
        except FileNotFoundError:
            pass  # no cache yet: the default labels
        except (OSError, ValueError) as error:
            warn("catalog %s: cannot read: %s; default labels used" % (cache, error))
        if raw is not None:
            try:
                parsed = catalog.parse(raw, faults)
            except ValueError as error:
                warn("catalog %s: %s; default labels used" % (cache, error))
        whole = parsed is not None and not faults
        for entry in raw["models"] if parsed else []:
            slug = entry.get("slug") if isinstance(entry, dict) else None
            if not isinstance(slug, str) or slug not in parsed:
                continue
            name = entry.get("display_name")
            if name is not None and (not isinstance(name, str) or not name.strip()):
                faults.append("%s: display_name %s is no label; the default label is used" % (slug, json.dumps(name)))
                name = None
            labels[slug] = name
        for fault in faults:
            warn("catalog %s: %s" % (cache, fault))
    for model in models:
        if model in settings.DEFAULT_PICKER_MODELS or model in labels:
            continue
        if whole:
            raise ValueError("%s: unknown model %s; not in the local ChatGPT catalog" % (where, model))
        warn("%s: model %s could not be checked against the catalog; kept with its default label" % (where, model))
    enabled = settings.Settings(path, environ, None).core(values)[4]
    if not enabled:
        return []
    return [{"model": model, "label": labels.get(model) or "GPT-" + model[4:].replace("-", " ").title(),
             "description": DESCRIPTION} for model in models]


def reconcile_picker(document, desired):
    """The exact description is the ownership marker; other rows and picker fields stay verbatim."""
    if not isinstance(document, dict):
        raise ValueError("Claude Code settings must be an object")
    if "modelPicker" not in document and not desired:
        return document
    picker = document.get("modelPicker", {})
    if not isinstance(picker, dict):
        raise ValueError("modelPicker must be an object")
    options = picker.get("options", [])
    if not isinstance(options, list):
        raise ValueError("modelPicker.options must be an array")
    foreign = [row for row in options if not (isinstance(row, dict) and row.get("description") == DESCRIPTION)]
    changed = dict(picker)
    if foreign or desired:
        changed["options"] = foreign + desired
    else:
        changed.pop("options", None)
    result = dict(document)
    if changed:
        result["modelPicker"] = changed
    else:
        result.pop("modelPicker", None)
    return result


def edit_picker(action, install_dir, target, backups, environ):
    """Back up before an actual atomic replacement, preserving target mode and any settings symlink."""
    desired = [] if action == "uninstall" else picker_rows(install_dir, environ)
    target = os.path.realpath(target)
    try:
        with open(target, encoding="utf-8") as source:
            document = json.load(source)
    except FileNotFoundError as error:
        if action == "uninstall":
            return
        raise ValueError("cannot read Claude Code settings at %s: %s" % (target, error)) from error
    except (OSError, ValueError) as error:
        raise ValueError("cannot read Claude Code settings at %s: %s" % (target, error)) from error
    result = reconcile_picker(document, desired)
    if action == "check" or result == document:
        return
    os.makedirs(backups, mode=0o700, exist_ok=True)
    mode = stat.S_IMODE(os.stat(target).st_mode)
    fd, backup = tempfile.mkstemp(prefix=settings.FILE_NAME + ".pre-" + action + "-", dir=backups)
    os.close(fd)
    shutil.copy2(target, backup)
    fd, temporary = tempfile.mkstemp(prefix=".settings-picker-", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), mode)
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    if len(sys.argv) != 5 or sys.argv[1] not in ("check", "install", "uninstall"):
        print("usage: picker.py check|install|uninstall INSTALL_DIR CLAUDE_SETTINGS BACKUPS", file=sys.stderr)
        return 2
    try:
        edit_picker(*sys.argv[1:], env.snapshot())
    except (OSError, ValueError) as error:
        print("gpt-gateway: model picker: %s" % error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
