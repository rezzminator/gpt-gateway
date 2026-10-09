"""The test jail: every scratch directory, port and environment a test uses comes from here.

Scratch directories share one prefix and go when the test ends. `clean_env` gives a subprocess a
scratch HOME and drops CODEX_HOME and every MODEL_GATEWAY_* variable the shell may carry, so no test reads or writes
the real home or a live gateway's settings.
"""
import json
import os
import re
import shutil
import socket
import tempfile
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "model-gateway-test-"


def scratch(test):
    """A fresh directory, removed when `test` ends."""
    path = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env(home, **overrides):
    """Drop CODEX_HOME and MODEL_GATEWAY_*; set HOME to `home`, then apply `overrides`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MODEL_GATEWAY_") and k != "CODEX_HOME"}
    env["HOME"] = home
    env.update(overrides)
    return env


def write_json(path, data, bump=0):
    """Write `data` (an object, or text as is); `bump` moves the mtime that many seconds past any earlier write."""
    with open(path, "w") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    if bump:
        st = os.stat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 10**9))


def write_middleware(directory, module, source):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, module + ".py")
    with open(path, "w") as f:
        f.write(textwrap.dedent(source))
    return path


def wait_for(check, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def ship_list():
    """The SHIP line of launchd/install.sh: what an install copies."""
    with open(os.path.join(ROOT, "launchd", "install.sh")) as f:
        found = re.search(r'^SHIP="([^"]*)"$', f.read(), re.M)
    return found.group(1).split()


def stage_install(dest):
    """An installed copy at `dest`, preserving package trees, with an empty middleware/; returns dest."""
    os.makedirs(dest, exist_ok=True)
    for item in ship_list():
        src, target = os.path.join(ROOT, item), os.path.join(dest, os.path.basename(item))
        if os.path.isdir(src):
            os.makedirs(target)
            for directory, _, names in os.walk(src):
                package = os.path.join(target, os.path.relpath(directory, src))
                for name in names:
                    if name.endswith(".py"):
                        os.makedirs(package, exist_ok=True)
                        shutil.copy(os.path.join(directory, name), package)
        else:
            shutil.copy(src, target)
    os.makedirs(os.path.join(dest, "middleware"), exist_ok=True)
    return dest
