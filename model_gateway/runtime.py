"""Runtime: the one object a running gateway holds — env, log, settings, hook chain, upstream, drain, sequence."""
from .capture import Sequence
from .env import CoreEnv
from .hooks import Chain
from .log import Log
from .server import Drain
from .settings import Settings
from .upstream import Upstream


class Runtime:
    """Built from an environment mapping and a settings path; `load()` is the strict start read."""

    def __init__(self, environ, settings_path):
        self.env = CoreEnv.parse(environ)
        self.log = Log(self.env.log_path)
        self.settings = Settings(settings_path, environ, self.log.write)
        self.upstream = Upstream(self.env.upstream, self.log.write)
        self.drain = Drain()
        self.sequence = Sequence()
        self.chain = Chain((), self.log.write)

    def load(self):
        """Read the settings and load the middleware; SettingsError or MiddlewareError stops the start."""
        self.settings.load()
        self.chain = Chain(self.settings.modules, self.log.write)
