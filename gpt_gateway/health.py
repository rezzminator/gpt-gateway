"""The health line `ok pid=N fds=OPEN/LIMIT capture=off|on settings=ok|missing|error(...) db=PATH db_rows=N db_drops=N db_errors=N
[chatgpt=on catalog=N models|none cache_retention=off|24h|24h(rejected) ]middleware=[NAME[ health] errors=N, ...]`."""
import os

from . import handles
from .hooks import Context

HEALTH_PATH = "/__gateway/health"


def health_line(snapshot, runtime):
    ctx = Context(log=lambda line: runtime.db.event("middleware_log", line=line), log_path=runtime.log.path,
                  settings=snapshot.module_settings)
    extra = ""
    if runtime.chatgpt is not None:
        count = runtime.chatgpt.catalog.count()
        extra = " chatgpt=on catalog=%s cache_retention=%s" % (
            "%d models" % count if count is not None else "none",
            runtime.chatgpt.retention_state(snapshot))
    used, limit = handles.open_count(), handles.soft_limit()
    return "ok pid=%d fds=%s/%s capture=%s settings=%s %s%s middleware=[%s]\n" % (
        os.getpid(), "?" if used is None else used, "unlimited" if limit is None else limit,
        "on" if snapshot.capture_dir else "off", snapshot.state, runtime.db.health(), extra,
        ", ".join(runtime.chain.health_entries(ctx)))
