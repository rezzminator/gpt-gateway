"""The health line `ok pid=N capture=off|on settings=ok|missing|error(...) middleware=[NAME[ health] errors=N, ...]`."""
import os

from .hooks import Context

HEALTH_PATH = "/__gateway/health"


def health_line(snapshot, chain, log, chatgpt=None):
    ctx = Context(log=log.write, log_path=log.path, settings=snapshot.module_settings)
    extra = ""
    if chatgpt is not None:
        count = chatgpt.catalog.count()
        extra = " chatgpt=on catalog=" + ("%d models" % count if count is not None else "none")
    return "ok pid=%d capture=%s settings=%s%s middleware=[%s]\n" % (
        os.getpid(), "on" if snapshot.capture_dir else "off", snapshot.state, extra,
        ", ".join(chain.health_entries(ctx)))
