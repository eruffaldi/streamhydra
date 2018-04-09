

import argparse
import mss
import subprocess
from ffmpy import FFmpeg
import time
from PIL import Image
import asyncio
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
#http://kyle.graehl.org/coding/2012/12/07/tornado-udpstream.html
#https://gist.github.com/jamiesun/1386e7a63663f0dd4d31

class FFmpegWriterLoop:
    def __init__(self,target):
        self.target = target
        self.q = queue.Queue()
        self.thread = threading.Thread(target=self.loop)
        self.thread.daemon = True
        self.thread.start()
    def loop(self):
        while True:
            data = self.q.get()
            if len(data) == 0:
                break
            self.target.write(data)
    def stop(self):
        self.q.put("")
    def write(self,x):
        self.q.put(x)
class ScreenGrabber:
    # Maximum Framerate by Python on OSX (2880x1800) is 7FPS
    def __init__(self,hub,parts=None,rate=30):
        self.sct = mss.mss()
        self.rate = rate
        self.consumers = []
        if parts is None:
            self.isfull = True
            self.parts = [(0,0,self.sct.monitors[1][0],self.sct.monitors[1][1])]
        else:
            self.isfull = False
            self.parts=parts
        self.pubs=[]
        for i in range(0,len(self.parts)):
            self.pubs.append(aiopubsub.Publisher(hub,aiopubsub.Key()))

    def grab(self):
        self.xgrabber = self.sct.grabber(self.sct.monitors[1])
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
            nxt = gen.sleep(1.0/self.rate)   # Start the clock.            
            ta = time.time()
            #print (n/(ta-tt))
            n = n + 1
            img = self.grab()
            if len(self.parts) > 1 or not self.isfull:
                pa = [img.crop((p[0],p[1],p[0]+p[2],p[1]+p[3])) for p in self.parts]
            else:
                pa = [img]
            for i,c,img in zip(range(0,len(pa)),self.pubs,pa):
                self.pubs[i].publish(aiopubsub.Key("area%d"%i),(ta,img))
            yield nxt 

class PILCompressor:
    def __init__(self,hub,name):
        self.name = name
        print ("PILCompressor subscribing to",name)
        self.jpegpub = aiopubsub.Publisher(hub, prefix = aiopubsub.Key(name,'jpeg'))
        self.subscriber = aiopubsub.Subscriber(hub,"")
        self.subscriber.subscribe(name)
        self.subscriber.add_listener(name, self.push,last=True)
    def push(self,key,ta_img):
        #print ("PIL received",img)
        now = time.time()
        ta = ta_img[0]
        buffer = io.BytesIO()
        ta_img[1].save(buffer, "JPEG")        
        #print ("pil",now-ta,ta)
        self.jpegpub.publish(aiopubsub.Key(),(ta_img[0],buffer.getvalue()))
        
class StreamingJpeg:
    def __init__(self):
        self.data = b"" # buffer
        self.buffer = b"" # for whole
    @gen.coroutine
    def ondata(self):
        data = self.data
        while len(data) != 0:
            k = data.find(b"\xFF\xD8")
            #print len(data),k,len(self.buffer)
            #print "jpeg",len(data),k
            if k < 0:
                self.buffer += data
                data = b""
                break
            else:
                self.buffer += data[0:k]
                if len(self.buffer) != 0:
                    yield self.buffer
                self.buffer = b"\xFF\xD8"
                data = data[k+2:]
        self.data = data

class ForwardStreamServer(TCPServer):
    def __init__(self):
        TCPServer.__init__(self)
        self.queue = asyncio.Queue()
    def ports(self):
        return [sock.getsockname()[1] for sock in self._sockets.values()]    
    async def handle_stream(self, stream, address):
        print ("someon connected to forwarder",address,stream)
        while True:            
            try:
                print ("wait")
                data = await self.queue.get()
                if len(data) == 0:
                    break
                print ("forwarding",len(data))
                yield stream.write(data)
            except StreamClosedError:
                break


class JpegStreamServer(TCPServer):
    def __init__(self,target):
        TCPServer.__init__(self)
        self.target = target
    def ports(self):
        return [sock.getsockname()[1] for sock in self._sockets.values()]
    @gen.coroutine
    def handle_stream(self, stream, address):
        print ("JpegStreamServer handle")
        q = StreamingJpeg()
        while True:
            try:
                # TODO read available
                data = yield stream.read(16384)
                print("read %d" % len(data))
                # parse the avaialble and eat until new jpeg 
                for y in q.ondata(data):
                    yield self.target(y)
                # the StreamingJpeg is the endpoint: link StreamingJpeg publisher to this publisher
            except StreamClosedError:
                break

class RTPStreamServer(UDPServer):
    def __init__(self,target):
        UDPServer.__init__(self)
        self.target = target
    @gen.coroutine
    def _on_receive(self, data, address):
        print ("received rtp packet",(len(data),address))
        yield self.target(data)

class FFmpegCompressor:
    def __init__(self,hub,name,sdpfilename,w,h,dojpeg=True,dortp=True,netmode=True,jpeginfo="",h264info=""):
        # create two UdpServer
        self.size = (w,h)
        self.name = name
        self.process = None
        print ("ffmpeg subscriber",name)
        self.subscriber = aiopubsub.Subscriber(hub,"")
        self.subscriber.subscribe(name)
        self.subscriber.add_listener(name, self.push,last=True)
        self.netmode = netmode

        if dortp:
            self.rtppub = aiopubsub.Publisher(hub, prefix = aiopubsub.Key(name,'rtp'))
            self.hserver = RTPStreamServer(self.onrtp)
            self.hserver.bind(0,family=socket.AF_INET)
            self.hserver.start()
            hports = self.hserver.ports()
            #h264info.split(" ") +
            rtppart =  ["-f","rtp","-sdp_file",sdpfilename] +  ["rtp://127.0.0.0:%d" % hports[0]]
        else:
            hports = []
            self.rtppub = None
            self.hserver = None
            rtppart = []
            # TODO expose: new JPEG packet event 

        if dojpeg:
            self.jpegpub = aiopubsub.Publisher(hub, prefix = aiopubsub.Key(name,'jpeg'))
            self.jserver = JpegStreamServer(self.onjpeg)
            self.jserver.listen(0)
            jports = self.jserver.ports()
            #jpeginfo.split(" ") +
            jpegpart = ["-f","mjpeg"] +  ["tcp://127.0.0.0:%d" % jports[0]]
            # TODO expose: new RTP packet event 
        else:
            jports = []
            self.jpegpub = None
            self.jserver = None
            jpegpart = []

        if not dojpeg and not dortp:
            self.args = None
            return
        else:
            beforesource = ["ffmpeg","-y","-f","rawvideo","-r","30","-pix_fmt","rgb32","-s","%dx%d" % (w,h)]
            print ("ffmpeg: ","jports",jports,"hports",hports)
            if not self.netmode:
                args = beforesource + ["-i","-"] + jpegpart + rtppart
            else:
                self.lserver = ForwardStreamServer()
                self.lserver.listen(0)
                lports = self.lserver.ports()
                args = beforesource + ["-i","tcp://127.0.0.0:%d" % lports[0]] + jpegpart + rtppart
            self.args = args
        self.tl = None
    @gen.coroutine
    def start(self):
        if not self.args:
            return
        print ("starting ffmpeg"," ".join(self.args))
        try:
            FNULL = open(os.devnull, 'w')
            self.process = subprocess.Popen(
                self.args,
                stdin=subprocess.PIPE,
                stdout=FNULL,#sys.stdout,#None,
                stderr=sys.stderr#subprocess.PIPE
            )
            #self.process.stderr.close()
            print ("started",self.args)
        except OSError as e:
            if e.errno == errno.ENOENT:
                raise Exception("Executable '{0}' not found".format("ffmpeg"))
            else:
                raise
        if not self.netmode:
            self.tl = FFmpegWriterLoop(self.process.stdin)
    @gen.coroutine
    def onjpeg(self,img):
        now = time.time()
        self.jpegpub.publish(aiopubsub.Key(),(now,img))

    @gen.coroutine
    def onrtp(self,pkt):
        now = time.time()
        self.rtppub.publish(aiopubsub.Key(),(now,pkt))
    @gen.coroutine
    def push(self,key,ta_img):
        #print ("ffmpeg push",key)
        if self.process is not None:
            ta,img = ta_img
            q = img.tobytes()
            #print ("ffmpeg receives image","process",img.size,"expected",self.size,len(q))
            if self.netmode:
                yield self.lserver.queue.put_nowait(q)
            else:
                if self.tl is not None:
                    self.tl.write(q)


class EndPointJPEG:
    def __init__(self):
        pass

class EndPointMJPEG:
    def __init__(self):
        pass

class EndPointH264:
    def __init__(self):
        pass

def area(s):
    try:
        x, y, w, h = map(int, s.split(','))
        return [x, y, w, h]
    except:
        raise argparse.ArgumentTypeError("Coordinates must be x,y,z,h")
def str2bool(v):
    if v.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif v.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')

class MainHandler(tornado.web.RequestHandler):
    def initialize(self,args,count):
        self.args = args
        self.count = count
    def get(self):
        self.write("found %d" % self.count)
        for i in range(0,self.count):
            block = """<a href='/jpeg%d'>jpeg</a> <a href='/mpeg%d'>mjpeg</a> <a href='/rtp%d'>rtp</a> <a href='/sdp%d'>sdp</a>
            """ % (i,i,i,i)
            self.write(block)

class UDPRTPPublisher:
    def __init__(self,hub,sourcename,udppublishers,host,port):
        self.hub = hub
        self.udppublishers = udppublishers
        self.target =  (host,port)
        w = udppublishers.get(self.target)
        if w is not None:
            w.stop()
        udppublishers[self.target] = self
        self.udp = UDPClient(host,port)
        self.key = sourcename
        self.subscriber = aiopubsub.Subscriber(hub,key)
        self.subscriber.add_listener(key, self.ondata)

    @gen.coroutine
    def ondata(self,x):
        yield self.udp.sendto(x)

    def stop(self):
        del self.udppublishers[self.target] # remove for future 
        self.subscriber.unsubscribe(self.key)

class RTPHandler(tornado.web.RequestHandler):
    def get(self,host,port,**kwargs):
        self.write("RTP entrypoint opener to %s:%d" % (host,port))
        qc = UDPRTPPublisher(kwargs["hub"],kwargs["name"],kwargs["udppublishers"],host,port)

class StopRTPHandler(tornado.web.RequestHandler):
    def get(self,host,port,**kwargs):
        q = (host,port)
        w = kwargs["udppublishers"].get(q)
        if w is not None:
            w.stop()
            self.write("stopping")
        else:
            self.clear()
            self.set_status(404) 
            self.finish("missing")

class SDPHandler(tornado.web.RequestHandler):
    def get(self,**kwargs):
        self.write(open(kwargs["filename"],"rb").read())

class ListRTPsHandler(tornado.web.RequestHandler):
    def get(self,**kwargs):
        for k,v in kwargs["udppublishers"].items():
            self.write("<br/>%s <a href='/stop/%s/%d'>Stop</a></br/>" % (k,k[0],k[1]))


class JpegHandler(tornado.web.RequestHandler):

    async def get(self):
        print ("jpeg request,waiting for",self.name)
        subscriber = aiopubsub.Subscriber(self.hub,"".join(self.name)+"jpeg")
        subscriber.subscribe(self.name)
        key, ta_compressedjpeg = await subscriber.consume()
        self._writeheaders(ta_compressedjpeg[0],len(ta_compressedjpeg[1]))
        self.write(ta_compressedjpeg[1])
    def initialize(self,hub,name):
        self.hub = hub
        self.name = name
    def _writeheaders(self,ta,n=None):
        self.set_status(200) # 200 OK http response
        self.set_header('Content-type', 'image/jpeg')
        if n is not None:
            self.set_header('Content-Length', "%d" % n)
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        self.set_header("Last-Modified", DT.datetime.utcfromtimestamp(ta).isoformat())

class MJpegHandler(tornado.web.RequestHandler):
    def initialize(self,hub,name):
        self.hub = hub
        self.name = name
        self.stop = False
    def on_connection_close(self):
        self.stop = True
    def options(self):
        print ("mjpeg options")
        self.set_status(200)
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Allow', 'GET')
        self.set_header("Access-Control-Allow-Origin","*")
        self.set_header("Access-Control-Allow-Methods","HEAD, GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers","Content-Type")
    @gen.coroutine
    def get(self):
        print ("mjpeg options")
        subscriber = aiopubsub.Subscriber(self.hub,self.name)
        subscriber.subscribe(self.name)
        self.set_status(200) # 200 OK http response
        self.set_header('Content-type', 'multipart/x-mixed-replace;boundary=BOUNDARY')
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        while True:
            # BUG this could be stuck
            key, ta_message = yield subscriber.consumelast()
            if self.stop:
                break
            now = time.time()
            ta = ta_message[0]
            print ("mjpeg",now-ta,ta)
            self.write(("\r\n--BOUNDARY\r\nContent-Type: image/jpeg\r\nX-TimeDelta:%f\r\nLast-Modified: %s\r\nX-TimeStamp: %f\r\nContent-Length: %d\r\n\r\n" % (now-ta,DT.datetime.utcfromtimestamp(ta).isoformat(),ta,len(ta_message[1]))).encode("ascii"))
            yield self.write(ta_message[1])
            yield self.flush()
        self.write(b"\r\n--BOUNDARY\r\n\r\n")
        yield self.flush()

def main():
    
    parser = argparse.ArgumentParser(description='Select streaming regions')
    parser.add_argument('--area',type=area,nargs="+",help="specify areas --area x1,y1,w1,h1 x2,y2,w2,h2 .... Not specified means full desktop",default=None)
    parser.add_argument('--http',type=int,default=8080,help="http port, use 0 for disable")
    parser.add_argument('--rtsp',type=int,default=8666,help="rtsp port, use 0 for disable")
    parser.add_argument('--rtp', type=str2bool, nargs='?',
                            const=True, default=True,help="support for RTP")
    parser.add_argument('--netmode', type=str2bool, nargs='?',
                            const=True, default=False,help="support for RTP")
    parser.add_argument('--jpegopt',help="jpeg options for ffmpeg",default="")
    parser.add_argument('--h264opt',help="h264 options for ffmpeg",default="")
    parser.add_argument('--mp4fragms',help="mp4 fragment millisconds",default=200,type=int)
    parser.add_argument('--grabrate',help="grabbing rate (Hz)",default=30,type=int)
    parser.add_argument("--piljpeg", type=str2bool, nargs='?',
                            const=True, default=False,help="uses PIL for jpeg computation")
    parser.add_argument('--rate',type=int,default=30,help="data rate")

    args = parser.parse_args()

    hub = aiopubsub.Hub()

    udppublishers = dict()

    # setup single grabber and splitter
    sc = ScreenGrabber(hub,args.area,rate=args.rate)
    handlers = [
             (r'/', MainHandler,dict(args=args,count=len(sc.parts)))
    ]
    ffmpegs = []
    for i in range(0,len(sc.parts)):
        print ("part is",sc.parts[i])
        sdpfilename = "area%d.sdp"%i        
        q = FFmpegCompressor(hub,"area%d"%i,sdpfilename=sdpfilename,w=sc.parts[i][2],h=sc.parts[i][2],netmode=args.netmode,dojpeg=not args.piljpeg,dortp=args.rtp,h264info=args.h264opt,jpeginfo=args.jpegopt)
        ffmpegs.append(q)
        if args.piljpeg:
            # PIL JPEG
            p = PILCompressor(hub,"area%d"%i)

        # publis image
        handlers.append(("/jpeg%d" % i, JpegHandler, dict(hub=hub,name=("area%d"%i,"jpeg"))))
        handlers.append(("/mjpeg%d" % i, MJpegHandler, dict(hub=hub,name=("area%d"%i,"jpeg"))))
        if args.rtp:
            # start and describe rtp like in RTSP
            handlers.append((r"/rtp%d/([^/]+)/(\d+)" % i, RTPHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
            handlers.append((r"/sdp%d" % i, SDPHandler, dict(filename=sdpfilename)))
    
    print ("finalizing")
    # stop and list RTPs (equivalent to RTSP)
    handlers.append((r"/stop/([^/]+)/(\d+)", StopRTPHandler, dict(hub=hub,udppublishers=udppublishers)))
    handlers.append((r"/rtps", ListRTPsHandler, dict(hub=hub,udppublishers=udppublishers)))

    app = web.Application(
        handlers,
        debug=True
    )
    print ("registerd",handlers)
    io = ioloop.IOLoop()
    server = HTTPServer(app)
    if args.http != 0:
        print ("listening on port",args.http)
        server.listen(args.http) 
    signal.signal(signal.SIGINT, lambda x, y: IOLoop.instance().stop())
    IOLoop.instance().spawn_callback(sc.grabber)
    IOLoop.instance().spawn_callback(lambda: [q.start() for q in ffmpegs])
    print ("loop starting")
    IOLoop.instance().start()

if __name__ == '__main__':
    main()