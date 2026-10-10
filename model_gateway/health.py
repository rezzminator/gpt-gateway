"""The health line `ok pid=N capture=off|on settings=ok|missing|error(...) db=PATH db_rows=N db_drops=N db_errors=N
[chatgpt=on catalog=N models|none ]middleware=[NAME[ health] errors=N, ...]`."""
import os

from .hooks import Context

HEALTH_PATH = "/__gateway/health"


def health_line(snapshot, runtime):
    ctx = Context(log=lambda line: runtime.db.event("middleware_log", line=line), log_path=runtime.log.path,
                  settings=snapshot.module_settings)
    extra = ""
    if runtime.chatgpt is not None:
        count = runtime.chatgpt.catalog.count()
        extra = " chatgpt=on catalog=" + ("%d models" % count if count is not None else "none")
    return "ok pid=%d capture=%s settings=%s %s%s middleware=[%s]\n" % (
        os.getpid(), "on" if snapshot.capture_dir else "off", snapshot.state, runtime.db.health(), extra,
        ", ".join(runtime.chain.health_entries(ctx)))
