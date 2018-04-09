from tornado.ioloop import IOLoop
from tornado.options import define, options
from tornado import gen
from tornado.iostream import StreamClosedError
from tornado.tcpclient import TCPClient
from tornado.tcpserver import TCPServer

define('port', default=0, help="TCP port to use")
define('server', default=False, help="Run as the echo server")
define('encoding', default='utf-8', help="String encoding")

class EchoServer(TCPServer):
    """Tornado asynchronous echo TCP server."""
    clients = set()
    
    @gen.coroutine
    def handle_stream(self, stream, address):
        ip, fileno = address
        print("Incoming connection from " + ip)
        EchoServer.clients.add(address)
        while True:
            try:
                yield self.echo(stream)
            except StreamClosedError:
                print("Client " + str(address) + " left.")
                EchoServer.clients.remove(address)
                break

    @gen.coroutine
    def echo(self, stream):
        data = yield stream.read_until('\n'.encode(options.encoding))
        print('Echoing data: ' + repr(data))
        yield stream.write(data)
    def ports(self):
        return [sock.getsockname()[1] for sock in self._sockets.values()]    

def start_server():
    server = EchoServer()
    server.listen(options.port)
    print("Starting server on tcp://localhost:" + str(server.ports()))
    IOLoop.instance().start()


start_server()