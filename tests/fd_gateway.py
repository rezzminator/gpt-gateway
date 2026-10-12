"""A gateway process under a lowered open-file limit, as `python fd_gateway.py LIMIT CLIENT_IDLE MAX_IDLE`.

The limit is set before the package loads and is both soft and hard, so the gateway can neither raise it nor start
cheaper than the descriptors it really needs. The two timing constants are shortened so a test waits seconds, not
minutes; a constant the code under test lacks is set harmlessly. The environment names the port, upstream, log and
database, as in every jail process.
"""
import os
import resource
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    limit = int(sys.argv[1])
    resource.setrlimit(resource.RLIMIT_NOFILE, (limit, limit))
    sys.path.insert(0, ROOT)
    from gpt_gateway import cli, pool, proxy
    proxy.CLIENT_IDLE_SECONDS = float(sys.argv[2])
    pool.MAX_IDLE = int(sys.argv[3])
    sys.exit(cli.main(["serve"], ROOT))
