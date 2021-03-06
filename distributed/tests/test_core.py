from functools import partial
from multiprocessing import Process
import socket

from tornado import gen, ioloop
import pytest

from distributed.core import read, write, pingpong, Server, rpc, connect
from distributed.utils_test import slow, loop

def test_server(loop):
    @gen.coroutine
    def f():
        server = Server({'ping': pingpong})
        server.listen(8887)

        stream = yield connect('127.0.0.1', 8887)

        yield write(stream, {'op': 'ping'})
        response = yield read(stream)
        assert response == b'pong'

        yield write(stream, {'op': 'ping', 'close': True})
        response = yield read(stream)
        assert response == b'pong'

        server.stop()

    loop.run_sync(f)


def test_rpc(loop):
    @gen.coroutine
    def f():
        server = Server({'ping': pingpong})
        server.listen(8887)

        remote = rpc(ip='127.0.0.1', port=8887)

        response = yield remote.ping()
        assert response == b'pong'

        response = yield remote.ping(close=True)
        assert response == b'pong'

        server.stop()

    loop.run_sync(f)


def test_rpc_with_many_connections(loop):
    remote = rpc(ip='127.0.0.1', port=8887)

    @gen.coroutine
    def g():
        for i in range(10):
            yield remote.ping()

    @gen.coroutine
    def f():
        server = Server({'ping': pingpong})
        server.listen(8887)

        yield [g() for i in range(10)]

        server.stop()

        remote.close_streams()
        assert all(stream.closed() for stream in remote.streams)

    loop.run_sync(f)


@slow
def test_large_packets(loop):
    """ tornado has a 100MB cap by default """
    def echo(stream, x):
        return x

    @gen.coroutine
    def f():
        server = Server({'echo': echo})
        server.listen(8887)

        data = b'0' * int(200e6)  # slightly more than 100MB

        conn = rpc(ip='127.0.0.1', port=8887)
        result = yield conn.echo(x=data)
        assert result == data

        server.stop()

    loop.run_sync(f)


def test_identity(loop):
    @gen.coroutine
    def f():
        server = Server({})
        server.listen(8887)

        remote = rpc(ip='127.0.0.1', port=8887)
        a = yield remote.identity()
        b = yield remote.identity()
        assert a['type'] == 'Server'
        assert a['id'] == b['id']

    loop.run_sync(f)


def test_ports(loop):
    port = 9876
    server = Server({})
    server.listen(port)
    try:
        assert server.port == port

        with pytest.raises((OSError, socket.error)):
            server2 = Server({})
            server2.listen(port)
    finally:
        server.stop()

    try:
        server3 = Server({})
        server3.listen(0)
        assert isinstance(server3.port, int)
        assert server3.port > 1024
    finally:
        server3.stop()
