"""Bind the Linux supervisor's shared listener, then exec its shell; startup failures use its fail-open cleanup."""
import signal

# A HUP during binding can wait: the shell we enter already starts the current code.
signal.signal(signal.SIGHUP, lambda signum, frame: None)

import os
import sys

REQUEST_QUEUE_SIZE = 256


def cleanup(signum=None, frame=None, error=None):
    try:
        os.close(3)
    except OSError:
        pass
    command = ['/bin/sh', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'run.sh')]
    if error is None:
        command += ['cleanup']
    else:
        command += ['failed', 'listener could not start: %s' % error]
    os.execv(command[0], command)


def main():
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    listener = None
    try:
        import socket
        port = int(sys.argv[1])
        command = sys.argv[2:]
        if not command:
            raise ValueError('missing supervisor command')
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        listener.bind(('127.0.0.1', port))
        listener.listen(REQUEST_QUEUE_SIZE)
        listener.setblocking(False)
        if listener.fileno() != 3:
            os.dup2(listener.fileno(), 3)
            listener.close()
        else:
            listener.detach()
        os.set_inheritable(3, True)
        os.execv(command[0], command)
    except (ImportError, SyntaxError, OSError, ValueError, OverflowError, IndexError) as error:
        if listener is not None:
            listener.close()
        cleanup(error=error)


if __name__ == '__main__':
    main()
