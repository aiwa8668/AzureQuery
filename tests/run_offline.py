import pathlib
import socket
import sys
import unittest
from unittest.mock import patch


def deny_network(*args, **kwargs):
    raise AssertionError("Network access forbidden in offline tests")


def run():
    root = pathlib.Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    original_exists = pathlib.Path.exists

    def safe_exists(path):
        return False if path.name == ".env" else original_exists(path)

    with patch.object(pathlib.Path, "exists", safe_exists), patch.object(socket.socket, "connect", deny_network), patch.object(socket.socket, "connect_ex", deny_network), patch.object(socket, "create_connection", deny_network):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


if __name__ == "__main__":
    raise SystemExit(not run())
