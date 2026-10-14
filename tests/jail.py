"""The test jail: every scratch directory, port and environment a test uses comes from here.

Scratch directories share one prefix and go when the test ends. `clean_env` gives a subprocess a
scratch HOME and drops CODEX_HOME and every GPT_GATEWAY_* variable the shell may carry, so no test reads or writes
the real home or a live gateway's settings.
"""
import json
import os
import re
import resource
import shutil
import socket
import tempfile
import textwrap
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "gpt-gateway-test-"
UNPRIVILEGED = 65534  # nobody, the id a root run demotes a permission test to


def scratch(test):
    """A fresh directory, removed when `test` ends."""
    path = os.path.realpath(tempfile.mkdtemp(prefix=PREFIX))
    test.addCleanup(shutil.rmtree, path, True)
    return path


def demote(test, *paths):
    """Root reads every file whatever its mode, so a test of an unreadable file runs as an unprivileged user there.
    Gives the scratch trees at `paths` to that user, opens the way down to them and into this checkout (whose code the
    child runs) for the length of `test`, and returns the `subprocess` arguments that run a child as it; `{}` when this
    process is not root, and the test runs as itself."""
    if os.geteuid() != 0:
        return {}
    for path in paths:
        for directory, names, files in os.walk(path):
            for name in [""] + names + files:
                os.lchown(os.path.join(directory, name), UNPRIVILEGED, UNPRIVILEGED)
    for start in [os.path.dirname(path) for path in paths] + [ROOT]:
        ancestor = start
        while ancestor != os.path.dirname(ancestor):
            mode = os.stat(ancestor).st_mode & 0o7777
            if not mode & 0o001:
                os.chmod(ancestor, mode | 0o001)
                test.addCleanup(os.chmod, ancestor, mode)
            ancestor = os.path.dirname(ancestor)
    return {"user": UNPRIVILEGED, "group": UNPRIVILEGED, "extra_groups": []}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def clean_env(home, **overrides):
    """Drop CODEX_HOME and GPT_GATEWAY_*; set HOME to `home` and the database inside it, then apply `overrides`."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GPT_GATEWAY_", "GIT_"))
           and k not in ("CODEX_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")}
    env["HOME"] = home
    env["GPT_GATEWAY_DB"] = os.path.join(home, "state", "gateway.db")
    env.update(overrides)
    return env


def write_secret(path, secret):
    """The install's route credential: `secret` in a 0600 file at `path`; returns the path."""
    write_json(path, secret)
    os.chmod(path, 0o600)
    return path


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


def raise_open_files(test, wanted):
    """Give this process room for `wanted` descriptors for the rest of `test`: macOS starts a shell's soft limit at
    256, which a test holding a hundred sockets and their hundred accepted ends cannot live in. A hard limit below
    `wanted` skips the test, saying so."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < wanted:
        if hard != resource.RLIM_INFINITY and hard < wanted:
            test.skipTest("the hard open-file limit is %d, under the %d this test needs" % (hard, wanted))
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        test.addCleanup(resource.setrlimit, resource.RLIMIT_NOFILE, (soft, hard))


def wait_for(check, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def ship_list():
    """The SHIP line of service/install.sh: what an install copies."""
    with open(os.path.join(ROOT, "service", "install.sh")) as f:
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
