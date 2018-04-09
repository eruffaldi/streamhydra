

import argparse
import subprocess
import time
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


class StreamingJpeg:
    def __init__(self):
        self.data = b"" # buffer
        self.buffer = b"" # for whole
    # generator NOT a coroutine
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
                data = yield stream.read_bytes(16384,partial=True)
                print("read %d" % len(data))
                # parse the avaialble and eat until new jpeg 
                q.data = q.data + data
                for y in q.ondata():
                    print("split %d"% len(y))
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


class Holder:
    def __init__(self):
        self.jpegpub = None
        self.rtppub = None

    @gen.coroutine
    def onjpeg(self,img):
        print("onjpeg",len(img))
        now = time.time()
        yield self.jpegpub.publish(aiopubsub.Key(),(now,img))

    @gen.coroutine
    def onrtp(self,pkt):
        print("onrtp",len(pkt))
        now = time.time()
        yield self.rtppub.publish(aiopubsub.Key(),(now,pkt))

def startffmpeg(args):
    print ("starting:"," ".join(args))
    try:
        FNULL = open(os.devnull, 'w')
        process = subprocess.Popen(
            args,
            shell=False,
            stdin=FNULL,
            stdout=sys.stdout,#None,
            stderr=FNULL#sys.stderr  #subprocess.PIPE
        )
        print ("spawned")
        #self.process.stderr.close()
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise Exception("Executable '{0}' not found".format("ffmpeg"))
        else:
            raise
    return  process
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

@tornado.web.stream_request_body
class MJpegInHandler(tornado.web.RequestHandler):
    def initialize(self,target,hub,name):
        self.hub = hub
        self.target = target
        self.name = name
        self.stop = False
        self.q = StreamingJpeg()
    def post(self):
        pass
    @gen.coroutine
    def data_received(self, chunk):
        print ("chunk",len(chunk))
        self.q.data += chunk
        for y in self.q.ondata():
            yield self.target(y)



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


def makeffmpeg_screen(input,parts,listeners,rtp,jpeg,rtpopts,jpegopts,inputrate):
    nocrop = False
    args = ["ffmpeg"]
    if inputrate != 0:
        args.append("-r")
        args.append(inputrate)
    if input == "screen":
        if sys.platform.startswith("win"):
            if False and len(parts) == 1: # optimize
                pa = "-offset_x %d -offset_y %d -video_size %dx%d" % (parts[0],parts[1],parts[2],parts[3])
                nocrop = True
            else:
                pa = ''
            command  = ['-f','gdigrab'] + [pa] + ['-i','desktop']
        else:   
            command = ['-f','avfoundation']
            if inputrate == 0:
                command.append('-r');
                command.append('30');
            command.append('-pix_fmt');
            command.append('nv12');
            command.append('-i');
            command.append('1');
        args.extend(command)
    else:
        args.append(input)

    if False and len(parts) == 1: # optimize
        if nocrop: # optimize
            pass

    else:
        targets = (1 if rtp else 0) + (1 if jpeg else 0)
        # complx
        #-filter_complex '[0:v]split=3[in1][in2][in3];[in1]crop=100:100:150:200[out1];[in2]crop=200:200:200:200[out2];[in3]crop=100:100:300:300[out3];[out1]split=2[out1A][out1B]'
        splitpart = "[0:v]split=%d" % len(parts) + "".join(["[in%d]" % i for i in range(0,len(parts))])
        fparts = [splitpart]
        for i,p in enumerate(parts):
            # crop directly to output
            if targets == 1:
                q = "A" if rtp else "B"
            else:
                q = ""
            fparts.append("[in%d]crop=%d:%d:%d:%d[out%d%s]" % (i,p[2],p[3],p[0],p[1],i,q))
            if targets == 2:
                fparts.append("[out%d]split=2[out%dA][out%dB]" % (i,i,i))
        args.append('-filter_complex')
        args.append(";".join(fparts))
        for i,p in enumerate(parts):
            if rtp:
                args.append("-map")
                args.append("[out%dA]" %i)
                args.append("-f")
                args.append("rtp")
                args.append("-sdp_file")
                args.append(listeners[i]["sdp"])
                if rtpopts != "":
                    args.append(rtpopts)
                args.append("rtp://127.0.0.1:%d" % listeners[i]["rtp"])
            if jpeg:
                args.append("-map")
                args.append("[out%dB]"% i)
                args.append("-f")
                args.append("mjpeg")
                if jpegopts != "":
                    args.append(jpegopts)
                if listeners[i]["jpeg"][0] == "tcp":
                    args.append("tcp://127.0.0.1:%d" % listeners[i]["jpeg"][1])
                else:
                    args.extend(['-chunked_post','1','-method','POST'])
                    args.append("http://127.0.0.1" +  listeners[i]["jpeg"][1])
    return args

def main():
    #ffplay area0.sdp -protocol_whitelist file,udp,rtp
    
    parser = argparse.ArgumentParser(description='Hydra capture')
    parser.add_argument('--area',type=area,nargs="+",help="space sparated regions (default is 0,0,800,600): --area x1,y1,w1,h1 x2,y2,w2,h2 ",default=[(0,0,800,600)])
    parser.add_argument('--http',type=int,default=8080,help="http port, use 0 for disabled (default 8080)")
    parser.add_argument('--rtsp',type=int,default=8666,help="rtsp port, use 0 for disabled (default 8666)")
    parser.add_argument('--rtp', type=str2bool, nargs='?',
                            const=True, default=True,help="enables RTP (default on)")
    parser.add_argument('--jpeg', type=str2bool, nargs='?',
                            const=True, default=True,help="enables JPEG (default on)")
    parser.add_argument('--jpegopts',help="extra mjpeg options for ffmpeg (e.g. quality)",default="")
    parser.add_argument('--rtpopts',help="extra rtp options for ffmpeg (e.g. encoder)",default="")
    parser.add_argument('--inputrate',help="grabbing rate (Hz) as -r to be put for the input (default 0)",default=0,type=int)
    parser.add_argument('--input',help="input value. Use 'screen' for desktop otherwise the ffmpeg input comprising the -i (default screen)",default="screen")
    parser.add_argument('--preferhttp',type=str2bool, nargs='?',
                            const=True, default=True,help="prefer http for internal connection with ffmpeg")
    args = parser.parse_args()

    if not args.jpeg and not args.rtp:
        print ("no ouputs selected")
        return
    if len(args.area) == 0:
        print ("no parts selected")
        return 

    parts = args.area
    hub = aiopubsub.Hub()

    udppublishers = dict()

    handlers = [
             (r'/', MainHandler,dict(args=args,count=len(parts)))
    ]
    ffmpegs = []

    listeners = {}
    for i in range(0,len(parts)):
        name ="area%d" %i
        sdpfilename = "area%d.sdp"%i        

        # UDP and TCP listeners
        listeners[i]={}
        holder = Holder()
        if args.rtp:
            rtppub = aiopubsub.Publisher(hub, prefix = aiopubsub.Key(name,'rtp'))
            hserver = RTPStreamServer(holder.onrtp)
            hserver.bind(0,family=socket.AF_INET)
            hserver.start()
            listeners[i]["rtp"] = hserver.ports()[0]
            listeners[i]["sdp"] = sdpfilename
            # start and describe rtp like in RTSP
            handlers.append((r"/rtp%d/([^/]+)/(\d+)" % i, RTPHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
            handlers.append((r"/sdp%d" % i, SDPHandler, dict(filename=sdpfilename)))
            holder.rtppub = rtppub

        if args.jpeg:
            jpegpub = aiopubsub.Publisher(hub, prefix = aiopubsub.Key(name,'jpeg'))
            if not args.preferhttp or args.http == 0:
                jserver = JpegStreamServer(holder.onjpeg)
                jserver.listen(0)
                listeners[i]["jpeg"] = ("tcp",jserver.ports()[0])
            else:
                listeners[i]["jpeg"] = ("http",":%d/jpegin%d" % (args.http,i))
                handlers.append(("/jpegin%d" % i, MJpegInHandler, dict(target=holder.onjpeg,hub=hub,name=("area%d"%i,"jpeg"))))

            holder.jpegpub = jpegpub

            # this could be used with:
            #  -chunked_post 1 -method POST -f rtp http://127.0.0.1:8080/x

            # publis image
            handlers.append(("/jpeg%d" % i, JpegHandler, dict(hub=hub,name=("area%d"%i,"jpeg"))))
            handlers.append(("/mjpeg%d" % i, MJpegHandler, dict(hub=hub,name=("area%d"%i,"jpeg"))))
    
    print ("preparing  ffmpegg")
    print (listeners)
    syntax = makeffmpeg_screen(args.input,parts,listeners,args.rtp,args.jpeg,args.rtpopts,args.jpegopts,args.inputrate)

    print (syntax)
    print (" ".join(syntax))

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
    IOLoop.instance().spawn_callback(lambda: startffmpeg(syntax))
    print ("loop starting")
    IOLoop.instance().start()

if __name__ == '__main__':
    main()