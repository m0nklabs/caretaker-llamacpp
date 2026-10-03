"""Fail closed on real I/O; tests must provide fakes or own their TCP listener."""

import asyncio
import ipaddress
import os
import socket
import subprocess
from contextlib import contextmanager


class UnsafeIOError(BaseException):
    """Escape production's broad Exception handlers rather than fake an outage."""


class IOGuard:
    def __init__(self):
        self.violations = []
        self.listeners = []

    def reject(self, operation, target):
        message = f"Unmocked {operation}: {target!r}; use a fake or a test-owned TCP listener"
        self.violations.append(message)
        raise UnsafeIOError(message)

    def assert_clean(self):
        """Make swallowed/task-local violations fail the owning test at teardown."""
        assert not self.violations, "Unmocked I/O attempted:\n" + "\n".join(self.violations)

    @contextmanager
    def allow_listener(self, server):
        """Permit only this live, loopback TCP server for the context's lifetime."""
        sockets = tuple(server.sockets or ())
        if not sockets:
            raise ValueError("Expected a running test-owned TCP server")
        for sock in sockets:
            if not ipaddress.ip_address(sock.getsockname()[0]).is_loopback:
                raise ValueError("Test listeners must bind loopback")
            if not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
                raise ValueError("Expected a listening TCP socket")
        self.listeners.extend(sockets)
        try:
            yield server
        finally:
            for sock in sockets:
                self.listeners.remove(sock)

    def permits(self, address):
        if not isinstance(address, tuple) or len(address) < 2:
            return False
        for sock in self.listeners:
            if sock.fileno() != -1 and sock.getsockname()[:2] == address[:2]:
                return True
        return False

    def install(self, monkeypatch):
        for name in ("connect", "connect_ex"):
            original = getattr(socket.socket, name)

            def connect(sock, address, _original=original):
                if sock.type != socket.SOCK_STREAM or not self.permits(address):
                    self.reject("socket connection", address)
                return _original(sock, address)

            monkeypatch.setattr(socket.socket, name, connect)

        original_getaddrinfo = socket.getaddrinfo

        def getaddrinfo(host, port, *args, **kwargs):
            # Numeric resolution is local; never send a DNS query from a unit test.
            try:
                ipaddress.ip_address(host)
            except ValueError:
                self.reject("DNS lookup", host)
            return original_getaddrinfo(host, port, *args, **kwargs)

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

        def forbidden(*args, **kwargs):
            self.reject("network/process I/O", args)

        async def forbidden_async(*args, **kwargs):
            forbidden(*args, **kwargs)

        for name in ("sendto", "sendmsg"):
            if hasattr(socket.socket, name):
                monkeypatch.setattr(socket.socket, name, forbidden)
        for name in ("gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
            monkeypatch.setattr(socket, name, forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_async)
        monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden_async)
        for name in ("system", "popen", "kill", "killpg", "fork", "posix_spawn", "posix_spawnp"):
            if hasattr(os, name):
                monkeypatch.setattr(os, name, forbidden)
