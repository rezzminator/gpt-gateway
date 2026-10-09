"""model-gateway's entry point: `/usr/bin/python3 -I gateway.py [serve] | check [FILE] | mw NAME [ARGS...]`.

Python's -I leaves this file's directory off sys.path, so it goes first here, and the model_gateway
package beside this file is the one that runs.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

if __name__ == "__main__":
    sys.path.insert(0, HERE)
    from model_gateway import cli
    sys.exit(cli.main(sys.argv[1:], HERE))
