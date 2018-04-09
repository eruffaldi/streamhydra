#!/usr/bin/env python
#coding=utf-8
#https://gist.github.com/jamiesun/1386e7a63663f0dd4d31
import socket
import os
import errno
from tornado.ioloop import IOLoop
from tornado.platform.auto import set_close_exec
import asyncio
class UDPClient(object):

    def __init__(self, host, port, loop=None):
        self._loop = asyncio.get_event_loop() if loop is None else loop
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setblocking(False)
        self._addr = (host, port)
        self._data = None

    def sendto(self, data):
        f = asyncio.Future(loop=self._loop)
        data = data if isinstance(data, bytes) else str(data).encode('utf-8')
        self._loop.add_writer(self._sock.fileno(), lambda: self._try_to_send(f,data) )
        return f

    def dsendto(self,data):
        self._sock.sendto(data, self._addr)

    def _try_to_send(self,f,d):
        try:
            self._sock.sendto(d, self._addr)
        except (BlockingIOError, InterruptedError):
            return
        except Exception as exc:
            f.set_exception(exc)
        else:
            if not f.done():
                f.set_result(True)

    def close(self):
        self._loop.remove_writer(self._sock.fileno())
        self._sock.close()

class UDPServer:
    def __init__(self, io_loop=None):
        self.io_loop = io_loop
        self._sockets = {}  # fd -> socket object
        self._pending_sockets = []
        self._started = False

    def ports(self):
        return [s.getsockname()[1] for s in self._sockets.values()]
    def add_sockets(self, sockets):
        if self.io_loop is None:
            self.io_loop = IOLoop.instance()

        for sock in sockets:
            self._sockets[sock.fileno()] = sock
            add_accept_handler(sock, self._on_receive,
                               io_loop=self.io_loop)

    def bind(self, port, address=None, family=socket.AF_UNSPEC, backlog=25):
        sockets = bind_sockets(port, address=address, family=family,
                               backlog=backlog)
        if self._started:
            self.add_sockets(sockets)
        else:
            self._pending_sockets.extend(sockets)

    def start(self, num_processes=1):
        assert not self._started
        self._started = True
        if num_processes != 1:
            process.fork_processes(num_processes)
        sockets = self._pending_sockets
        self._pending_sockets = []
        self.add_sockets(sockets)

    def stop(self):
        for fd, sock in self._sockets.iteritems():
            self.io_loop.remove_handler(fd)
            sock.close()

    def _on_receive(self, data, address):
        print ("UDPServer RECEIVE",address)

def bind_sockets(port, address=None, family=socket.AF_UNSPEC, backlog=25):
    sockets = []
    if address == "":
        address = None
    flags = socket.AI_PASSIVE
    if hasattr(socket, "AI_ADDRCONFIG"):
        flags |= socket.AI_ADDRCONFIG
    for res in set(socket.getaddrinfo(address, port, family, socket.SOCK_DGRAM,
                                  0, flags)):
        af, socktype, proto, canonname, sockaddr = res
        sock = socket.socket(af, socktype, proto)
        set_close_exec(sock.fileno())
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if af == socket.AF_INET6:
            if hasattr(socket, "IPPROTO_IPV6"):
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.setblocking(0)
        sock.bind(sockaddr)
        sockets.append(sock)
    return sockets

if hasattr(socket, 'AF_UNIX'):
    def bind_unix_socket(file, mode=0x180, backlog=128):#0600 octal
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        set_close_exec(sock.fileno())
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setblocking(0)
        try:
            st = os.stat(file)
        except OSError as err:
            if err.errno != errno.ENOENT:
                raise
        else:
            if stat.S_ISSOCK(st.st_mode):
                os.remove(file)
            else:
                raise ValueError("File %s exists and is not a socket", file)
        sock.bind(file)
        os.chmod(file, mode)
        sock.listen(backlog)
        return sock


def add_accept_handler(sock, callback, io_loop=None):
    if io_loop is None:
        io_loop = IOLoop.instance()

    def accept_handler(fd, events):
        while True:
            try:
                data, address = sock.recvfrom(16384)
            except socket.error as e:
                if e.args[0] in (errno.EWOULDBLOCK, errno.EAGAIN):
                    return
                raise
            callback(data, address)
    io_loop.add_handler(sock.fileno(), accept_handler, IOLoop.READ)

if __name__ == '__main__':
    serv = UDPServer()
    serv.bind(0,family=socket.AF_INET)
    serv.start()
    print (serv.ports())
    IOLoop.instance().start()