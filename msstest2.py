
import mss
import subprocess
from ffmpy import FFmpeg
import time
from PIL import Image
import signal
import socket
from tornado import web, gen,ioloop
from tornado.options import options
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.iostream import StreamClosedError
from tornado_udp import UDPServer
from tornado.tcpserver import TCPServer
from tornado.iostream import StreamClosedError
from tornado import gen

#http://kyle.graehl.org/coding/2012/12/07/tornado-udpstream.html
#https://gist.github.com/jamiesun/1386e7a63663f0dd4d31

class ScreenGrabber:
    # Maximum Framerate by Python on OSX (2880x1800) is 7FPS
    def __init__(self,parts=None):
        self.sct = mss.mss()
        self.parts=None
        self.consumersCount = 1 if self.parts is None else len(self.parts)
        self.consumers = []
        self.xgrabber = self.sct.grabber(self.sct.monitors[1])
    def grab(self):
        sct_img = self.xgrabber.grab()
        #https://pillow.readthedocs.io/en/latest/handbook/concepts.html#concept-modes
        #alternatively using numpy
        img = Image.frombytes('RGBX', sct_img.size, sct_img.bgra) # actually BGRA
        return img
    @gen.coroutine
    def grabber(self):
        # check if consumer read
        tt = time.time()
        n = 1
        while True:
            nxt = gen.sleep(1.0/100)   # Start the clock.            
            ta = time.time()
            print n/(ta-tt)
            n = n + 1
            img = self.grab()
            if self.parts is not None:
                pa = [img.crop(p) for p in self.parts]
            else:
                pa = [img]
            for c,img in zip(self.consumers,pa):
                yield c.push(img)
            yield nxt 


class FFmpegCompressor:
    def __init__(self,name,jpeginfo,h264info):
        # create two UdpServer
        self.name = name
        self.jserver = UDPServer()
        self.hserver = UDPServer()
        self.jserver.bind(0,family=socket.AF_INET)
        self.hserver.bind(0,family=socket.AF_INET)
        self.jserver.start()
        self.hserver.start()
        # start ffmpeg 
        pass
    @gen.coroutine
    def push(self,img):
        print ("received",img)

    @gen.coroutine
    def onmjpeg(self):
        pass
    @gen.coroutine
    def onh264(self):
        pass

class EndPointJPEG:
    def __init__(self):
        pass

class EndPointMJPEG:
    def __init__(self):
        pass

class EndPointH264:
    def __init__(self):
        pass

def main():
    # setup single grabber and splitter
    publishers = dict()
    sc = ScreenGrabber()
    for i in range(0,sc.consumersCount):
        q = FFmpegCompressor("ffmpeg","","")
        sc.consumers.append(q)
        #publishers["%d/jpeg" % i] = q.jpeg
        #publishers["%d/rtp" % i] = q.rtp

    app = web.Application(
        [
         #   (r'/', MainHandler),
         #   (r'/events', EventSource, dict(source=publisher))
        ],
        debug=True
    )
    # TODO add RTSP 
    io = ioloop.IOLoop()
    server = HTTPServer(app)
    server.listen(8080) 
    signal.signal(signal.SIGINT, lambda x, y: IOLoop.instance().stop())
    #ioloop.PeriodicCallback(sc.next, 33, io).start()
    IOLoop.instance().spawn_callback(sc.grabber)
    IOLoop.instance().start()

if __name__ == '__main__':
    main()