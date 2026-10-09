"""The health line `ok pid=N capture=off|on settings=ok|missing|error(...) middleware=[NAME[ health] errors=N, ...]`."""
import os

from .hooks import Context

HEALTH_PATH = "/__gateway/health"


def health_line(snapshot, chain, log):
    ctx = Context(log=log.write, log_path=log.path, settings=snapshot.module_settings)
    return "ok pid=%d capture=%s settings=%s middleware=[%s]\n" % (
        os.getpid(), "on" if snapshot.capture_dir else "off", snapshot.state,
        ", ".join(chain.health_entries(ctx)))
