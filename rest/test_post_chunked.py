#
import argparse
import subprocess
import time
import signal
import socket
import tornado.web
import io
import sys
import datetime as DT
from tornado import web, gen,ioloop
from tornado.options import options
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import StreamClosedError
from tornado_udp import UDPServer,UDPClient
from tornado.tcpserver import TCPServer
from tornado.iostream import StreamClosedError
from tornado import gen
import aiopubsub
import threading
import queue
import os

@tornado.web.stream_request_body
class SampleChunkedHandler(web.RequestHandler):
    def post(self):
        print("done",self.request.headers)

    def data_received(self, chunk):
        print (len(chunk))
        open("x.jpeg","wb").write(chunk)
        sys.exit(0)

def main():
    # curl -v -H "Transfer-Encoding: chunked" --data-binary @somefile http://127.0.0.1:8080/x
    # ffmpeg -i x.mp4 -chunked_post 1 -method POST -f mjpeg http://127.0.0.1:8080/x

    handlers = [
             (r'/x', SampleChunkedHandler)
    ]
    app = web.Application(
            handlers,
            debug=True
        )

    io = ioloop.IOLoop()
    server = HTTPServer(app)
    server.listen(8080) 
    IOLoop.instance().start()


main()