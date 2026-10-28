"""Runtime: the one object a running gateway holds — env, log, database, settings, hook chain, upstream, host pause
watch, drain, sequence."""
from .capture import Sequence
from .db import Db
from .env import CoreEnv, ROUTE_SECRET_PATH, route_secret
from .hooks import Chain
from .log import Log, text_path
from .reach import Pause
from .server import Drain
from .settings import Settings
from .upstream import Upstream


class Runtime:
    """Built from an environment mapping and a settings path; `load()` is the strict start read."""

    def __init__(self, environ, settings_path):
        self.env = CoreEnv.parse(environ)
        self.log = Log(text_path(environ), environ=environ)  # start, stop, fatal and settings-error lines, and the database's failures
        self.db = Db(self.env.db_path, self.log.write, lambda: self.settings.snapshot.db_retention_days)  # no reload on its thread
        self.settings = Settings(settings_path, environ, self.log.write, self.db.event)
        self.upstream = Upstream(self.env.upstream, self.db.event)
        self.pause = Pause(self.db.event)  # the host's sleeps, seen by every request
        self.drain = Drain()
        self.sequence = Sequence()
        self.chain = Chain((), self.db.event)
        self.chatgpt = None
        self.route_secret = ""  # the install's local-client credential; "" when none is installed

    def load(self):
        """Read the settings, load the middleware and start the database writer; SettingsError, MiddlewareError or
        EnvError stops the start."""
        self.settings.load()
        self.chain = Chain(self.settings.modules, self.db.event)
        if self.settings.snapshot.chatgpt:
            from .env import ChatGPTEnv
            from .chatgpt.route import Route
            knobs = ChatGPTEnv.parse(self.settings.env)
            self.route_secret = route_secret(self.settings.env, required=True)
            # The token store opens on first use, so a Codex login never waits on a Keychain probe (K11).
            self.chatgpt = Route(knobs, self.db.event)
        elif ROUTE_SECRET_PATH in self.settings.env:
            self.route_secret = route_secret(self.settings.env)
        self.db.start()
