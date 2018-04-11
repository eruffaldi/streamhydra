# TODO:
# back RTSP connection
# muxing RTP H264  into ISO BMFF (MP4) fragments. e.g. https://github.com/Streamedian/html5_rtsp_player
import sys
import os
sys.path.insert(1, os.path.abspath(os.path.split(sys.argv[0])[0]))

try:
    from termcolor import colored
except:
    colored = lambda x,y: x

import struct
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
from tornado import web, gen,ioloop,websocket
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
import asyncio
import queue
import os
from urllib.parse import urlparse
import json 

def strbyte(a):
    return str(a).encode("latin1")
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

def parseH264header(packet):
    #http://www.netlab.tkk.fi/opetus/s383152/2010/2-rtp-b.pdf
    #https://www.itu.int/rec/T-REC-H.264-201704-I/en
    #Table 7-1 – NAL unit type codes, syntax element categories, and NAL unit type classes
    b = ord(packet[0])
    F = b & 0x80
    NRI = b & 0x60 
    NALUtype = b & 0x1F
    if F == 0:
        return None
    # packet is padded to be aligned
    else:
        return dict(discardableNRI=NRI,nalutype=NALUtype,body=packet[1:])
def parsePacket(packet):
    # from https://github.com/sparkslabs/kamaelia
    e = struct.unpack(">BBHII",packet[:12])
    
    if (e[0]>>6) != 2:       # check version is 2
        return None
    
    # ignore padding bit atm
    
    hasPadding   = e[0] & 0x20
    hasExtension = e[0] & 0x10
    numCSRCs     = e[0] & 0x0f
    hasMarker    = e[1] & 0x80
    payloadType  = e[1] & 0x7f
    seqnum       = e[2]
    timestamp    = e[3]
    ssrc         = e[4]
    
    i=12
    if numCSRCs:
        csrcs = struct.unpack(">"+str(numCSRCs)+"I", packet[i:i+4*csrcs])
        i=i+4*numCSRCs
    else:
        csrcs = []
        
    if hasExtension:
        ehdr, length = struct(">2sH",packet[i:i+4])
        epayload = packet[i+4:i+4+length]
        extension = (ehdr,epayload)
        i=i+4+length
    else:
        extension = None
    
    # now work out how much padding needs stripping, if at all
    end = len(packet)
    if hasPadding:
        amount = ord(packet[-1])
        end = end - amount
        
    payload = packet[i:end]
    
    return ( seqnum,
             { 'payloadtype' : payloadType,
               'payload'     : payload,
               'timestamp'   : timestamp,
               'ssrc'        : ssrc,
               'extension'   : extension,
               'csrcs'       : csrcs,
               'marker'      : hasMarker,
             }
           )

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

class RTPSSession:
    def __init__(self):
        self.udppublisher = None
        self.lastcseq = 0
        self.ssrc = None
        self.session = 0
        self.path = None
        self.target = None

class RTCPServer(TCPServer):
    def __init__(self,rtspserver):
        TCPServer.__init__(self)
        self.rtspserver = rtspserver
    @gen.coroutine
    def handle_stream(self, stream, address):
        while True:
            try:
                print ("RTCP connected by address",address)
            except:
                pass

class RTSPServer(TCPServer):
    def __init__(self,hub,udppublishers,ports):
        TCPServer.__init__(self)
        self.ports = ports
        self.hub = hub
        self.udppublishers = udppublishers    
        self.sessions = {}
        self.lastsession = 1
        self.ports = ports
    @gen.coroutine
    def handle_stream(self, stream, address):
        while True:
            try:
                #UDPRTPPublisher(self.kwargs["hub"],self.kwargs["name"],self.kwargs["udppublishers"],host,port)
                data = yield stream.read_until(b"\r\n\r\n",max_bytes=1024)
                #print ("RTSP request from",address,data)
                lines = data.strip().split(b"\r\n")
                first = lines[0]
                firstparts = first.split(b" ")
                if len(firstparts) < 3:
                    break
                method = firstparts[0]
                url = firstparts[1]
                urlparts = urlparse(url)
                proto = firstparts[2]
                headers = dict([[y.strip() for y in x.split(b":",1)] for x in lines[1:]])
                cseq = headers.get(b"CSeq",b"0").strip(b" ")
                try:
                    session = int(headers.get(b"Session",b"-1"))
                except:
                    session = -1
                osession = self.sessions.get(session)

                #https://en.wikipedia.org/wiki/Real_Time_Streaming_Protocol
                if proto != b"RTSP/1.0" or urlparts.scheme != b"rtsp":
                    print ("BAD proto is ",proto,"and scheme is",urlparts.scheme)
                    return
                if not urlparts.path.startswith(b"/area"):
                    print ("BAD start url not /area")
                    self.sendresponse(stream,404,b"NOT FOUND only /area#",cseq)
                    return
                try:
                    area = int(urlparts.path[5:])
                except:
                    area = 0

                print(colored("RTSP parsed request","red")," from",address,colored(method,"red"),url,"headers",headers,"cseq <",cseq,"> session",session,"osssion",osession)

                if method == b"OPTIONS":
                    self.sendresponse(stream,200,b"OK",cseq,headers={b"Public":b"DESCRIBE, SETUP, TEARDOWN, PLAY, PAUSE"})
                elif method == b"DESCRIBE":
                    self.sendcontent(stream,makesdp(b"area%d.sdp" % area,None,0),b"application/sdp",cseq=cseq)
                elif method == b"SETUP":
                    # CSeq
                    # TRANSPORT Transport: RTP/AVP;unicast;client_port=8000-8001
                    t = headers.get(b"Transport",b"")
                    if not t.startswith(b"RTP/AVP/UDP") or not t.find(b"unicast") >= 0 or not t.find(b"client_port") >= 0:
                        self.sendresponse(stream,500,b"BAD request SETUP",cseq=cseq) # %s %s %s" % (t.startswith(b"RTP/AVP"),t.find(b"unicast"),t.find(b"client_port")))
                    else:
                        at = t.split(b";")
                        ports = [p.split(b"=")[1] for p in at if p.startswith(b"client_port=")]
                        if len(ports) > 0:
                            host = address
                            port = int(ports[0].split(b"-")[0])

                            s = RTPSSession()
                            session = self.lastsession 
                            s.ssrc = b"%d" % session
                            s.udppublisher = UDPRTPPublisher(self.hub,b"area%d" % area,self.udppublishers,host,port,paused=True)
                            transport = b"RTP/AVP;unicast;client_port=%s;server_port=%s;ssrc=%s" % (ports[0],self.ports.encode("ascii"),s.ssrc)
                            self.sessions[session] = s
                            self.lastsession  += 1
                            self.sendresponse(stream,200,b"OK",cseq,session,headers={b"Transport": transport})
                        else:
                            print("RTSP 500 NO ports in",at)
                            self.sendresponse(stream,500,b"NO PORTS...",cseq,session)
                            pass
                    # block interleaved=0-1
                    # Transport: RTP/AVP;unicast;client_port=8000-8001;server_port=9000-9001;ssrc=1234ABCD
                elif method == b"PLAY":
                    if osession is not None:
                        osession.udppublisher.paused = False
                        # TODO: respond RTP-Info: url=rtsp://example.com/media.mp4/streamid=0;seq=9810092;rtptime=3450012
                        rtpinfo = b"url=%s;seq=%d;rtptime=%d" % (url,0,0)
                        self.sendresponse(stream,200,b"OK",cseq,session,headers={b"RTP-Info":rtpinfo})
                    else:
                        self.sendresponse(stream,404,b"Unknown session %d" % session,cseq)
                elif method == b"SESSIONS":
                    self.sendcontent(stream,b"\r\n".join([b"%d -> %s:%s" % (x,strbyte(self.sessions[x].udppublisher.target[0]),strbyte(self.sessions[x].udppublisher.target[1])) for x in self.sessions.keys()]),contenttype=b"text/plain",cseq=cseq)                    
                elif method == b"PAUSE":
                    if osession is not None:
                        osession.udppublisher.paused = True
                        self.sendresponse(stream,200,b"OK",cseq,session)
                    else:
                        self.sendresponse(stream,404,b"Unknown session %d" % session,cseq)
                elif method == b"TEARDOWN":
                    if osession is not None:
                        osession.udppublisher.stop()
                        del self.sessions[session]
                        self.sendresponse(stream,200,b"OK",cseq,session)
                    else:
                        self.sendresponse(stream,404,b"Unknown session %d" % session,cseq)
                else:
                    self.sendresponse(stream,404,b"Unknown Method " + method,cseq)
                #ANNOUNCE
                #GET_PARAMETER e.g. packets_received jitter as text/parameters
                #SET_PARAMETER
                #REDIRECT
                #RECORD
            except:
                raise

    @gen.coroutine
    def writeresponse(self,stream,code,text):
        print(colored("RTSP response:","green"),code,text)
        yield stream.write(b"RTSP/1.0 %d %s\r\n" % (code,text))
    @gen.coroutine
    def writeheaders(self,stream,headers,close=True):
        print ("RTSP writeheaders",headers)
        h = b"".join([b"%s: %s\r\n" % (k,v) for k,v in headers.items()])
        if close:
            h += b"\r\n"
        print ("RTSP header text",h)
        yield stream.write(h)
    @gen.coroutine
    def closehead(self,stream):
        yield stream.write(b"\r\n\r\n")
    @gen.coroutine
    def sendresponse(self,stream,code,text,cseq,session=None,headers=None):
        self.writeresponse(stream,code,text)
        h = {b"CSeq": cseq}
        if session is not None:
            h[b"Session"] = strbyte(session)
        if headers is not None:
            h.update(headers)
        self.writeheaders(stream,h)

    @gen.coroutine
    def sendcontent(self,stream,content,contenttype,cseq,session=None,headers=None):
        self.writeresponse(stream,200,b"OK")
        h = {b"CSeq": cseq}
        if session is not None:
            h[b"Session"] = strbyte(session)
        h[b"Content-Length"] = strbyte(len(content))
        if contenttype is not None:
            h[b"Content-Type"]  = contenttype
        if headers is not None:
            h.update(headers)
        self.writeheaders(stream,h)
        print ("RTSP content",content)        
        yield stream.write(content)

class JpegStreamServer(TCPServer):
    def __init__(self,target):
        TCPServer.__init__(self)
        self.target = target
    def ports(self):
        return [sock.getsockname()[1] for sock in self._sockets.values()]
    @gen.coroutine
    def handle_stream(self, stream, address):
        q = StreamingJpeg()
        while True:
            try:
                # TODO read available
                data = yield stream.read_bytes(16384,partial=True)
                # parse the avaialble and eat until new jpeg 
                q.data = q.data + data
                for y in q.ondata():
                    print("split %d"% len(y))
                    yield self.target(y)
                # the StreamingJpeg is the endpoint: link StreamingJpeg publisher to this publisher
            except StreamClosedError:
                break

class UDPServerToCoroutine(UDPServer):
    def __init__(self,target):
        UDPServer.__init__(self)
        self.target = target
    @gen.coroutine
    def _on_receive(self, data, address):
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
        now = time.time()
        yield self.jpegpub.publish(aiopubsub.Key(),(now,img))

    @gen.coroutine
    def onrtp(self,pkt):
        #print("onrtp",len(pkt))
        now = time.time()
        yield self.rtppub.publish(aiopubsub.Key(),(now,pkt))

def startffmpeg(args,verbose=False):
    print ("starting:"," ".join(args))
    try:
        FNULL = open(os.devnull, 'w')
        if not verbose and sys.platform.startswith("win"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        else:
            startupinfo = None
        process = subprocess.Popen(
            args,
            shell=False,
            stdin=FNULL,
            stdout=sys.stdout if verbose else FNULL,#None,
            stderr=sys.stderr if verbose else FNULL,#sys.stderr  #subprocess.PIPE
            startupinfo=startupinfo
        )
        print ("spawned")
        #self.process.stderr.close()
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise Exception("Executable '{0}' not found".format("ffmpeg"))
        else:
            raise
    return  process

class UDPRTPPublisher:
    def __init__(self,hub,sourcename,udppublishers,host,port,paused=False):
        self.hub = hub
        self.paused = paused
        self.rtcp = False
        self.udppublishers = udppublishers
        self.target =  (host,port)
        self.started = time.time()
        w = udppublishers.get(self.target)
        if w is not None:
            print("UDP stopping existing UDP target",self.target)
            w.stop()
        udppublishers[self.target] = self
        self.udp = UDPClient(host,port)
        self.key = sourcename
        print(colored("UDPRTPPublisher","green"))
        self.subscriber = aiopubsub.Subscriber(hub,self.key)
        self.subscriber.add_listener(self.key, self.ondata)
        print(colored("UDPRTPPublisher subscribed","green"))

    @gen.coroutine
    def ondata(self,key,x):
        if not self.paused:
            #if not self.rtcp and (time.time()-self.started) > 1800:
            #    self.stop()
            #else:
            yield self.udp.sendto(x[1])
        else:
            print (".",end="")
    def stop(self):
        print ("stopping publisher",self.target,self.udp,self)
        del self.udppublishers[self.target] # remove for future 
        self.subscriber.unsubscribe(self.key)

def makesdp(name,host,port):
    lines = open(name,"rb").read().split(b"\n")
    olines = []
    for i,l in enumerate(lines):
        if l.startswith(b"SDP:"):
            continue
        if l.startswith(b"m=video"):
            #m=video 52645 RTP/AVP 96
            a = l.split(b" ")
            a[1] = strbyte(port)
            olines.append(b" ".join(a))
        elif host is None:
            if l.startswith(b"c=") or l.startswith(b"o="):
                continue
            else:
                olines.append(l)
        else:
            if l.startswith(b"c="):
                a = l.split(b" ")
                a[2] = host.encode("ascii")
                olines.append(b" ".join(a))
            else:
                olines.append(l)
    return b"\n".join(olines) 
class RTPHandler(tornado.web.RequestHandler):
    def initialize(self,**kwargs):
        self.kwargs = kwargs

class RTPChunkedHandler(tornado.web.RequestHandler):
    def initialize(self,**kwargs):
        self.kwargs = kwargs
        self.stop = False
    def on_connection_close(self):
        self.stop = True
    @gen.coroutine
    def get(self):
        subscriber = aiopubsub.Subscriber(self.kwargs["hub"],self.kwargs["name"])
        subscriber.subscribe(self.kwargs["name"])
        self.set_status(200) # 200 OK http response
        self.set_header('Content-type', 'application/rtp')
        self.set_header("Transfer-Encoding", "chunked")
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        while True:
            # BUG this could be stuck
            key, ta_message = yield subscriber.consumelast()
            if self.stop:
                break
            now = time.time()
            ta = ta_message[0]
            self.writechunk(ta_message[1])
    @gen.coroutine
    def writechunk(self,chunk):
        tosend = b'%X\r\n'%(len(chunk))
        yield self.write(tosend+chunk+b"\r\n")
        yield self.flush()

class RTPDumpHandler(tornado.web.RequestHandler):
    def initialize(self,**kwargs):
        self.kwargs = kwargs
        self.stop = False
    def on_connection_close(self):
        self.stop = True
    @gen.coroutine
    def get(self):
        subscriber = aiopubsub.Subscriber(self.kwargs["hub"],self.kwargs["name"])
        subscriber.subscribe(self.kwargs["name"])
        self.set_status(200) # 200 OK http response
        self.set_header('Content-type', 'application/json')
        self.set_header("Transfer-Encoding", "chunked")
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Connection', 'keep-alive')
        while True:
            # BUG this could be stuck
            key, ta_message = yield subscriber.consumelast()
            if self.stop:
                break
            now = time.time()
            ta = ta_message[0]
            pa = parsePacket(ta_message[1])
            if pa is None:
                pa = ("Invalid",len(ta_message[1]))
            else:
                pa[1]["payload"] = len(pa[1]["payload"])
            self.writechunk(json.dumps(pa).encode("latin1")+b"\r\n")
    @gen.coroutine
    def writechunk(self,chunk):
        tosend = b'%X\r\n'%(len(chunk))
        yield self.write(tosend+chunk+b"\r\n")
        yield self.flush()
class RTPWSHandler(tornado.websocket.WebSocketHandler):
    def initialize(self,**kwargs):
        self.kwargs = kwargs
        self.stop = False
    def check_origin(self, origin):
        return True
    def on_close(self):
        self.stop = True
    @gen.coroutine
    def open(self):
        subscriber = aiopubsub.Subscriber(self.kwargs["hub"],self.kwargs["name"])
        subscriber.subscribe(self.kwargs["name"])
        # TODO: how to send 
        while True:
            # BUG this could be stuck
            key, ta_message = yield subscriber.consumelast()
            if self.stop:
                break
            now = time.time()
            ta = ta_message[0]
            self.write_message(ta_message[1])
        self.close()

#https://tools.ietf.org/html/rfc4566
class SDPHandlerCustom(tornado.web.RequestHandler):
    def initialize(self,filename):
        self.filename = filename
    def get(self,host,port):
        self.write(makesdp(self.filename,host,port))



class StopRTPHandler(tornado.web.RequestHandler):
    def get(self,host,port):
        q = (host,port)
        w = kwargs["udppublishers"].get(q)
        if w is not None:
            w.stop()
            self.write("stopping publisher %s:%d" % q)
        else:
            self.clear()
            self.set_status(404) 
            self.finish("missing")

class SDPHandler(tornado.web.RequestHandler):
    def initialize(self,filename):
        self.filename = filename
    def get(self):
        self.write(open(self.filename,"rb").read())


class QuitHandler(tornado.web.RequestHandler):
    def get(self):
        self.write("bye bye")
        self.flush()
        ioloop = tornado.ioloop.IOLoop.instance()
        ioloop.add_callback(ioloop.stop)

class ListRTPsHandler(tornado.web.RequestHandler):
    def initialize(self,udppublishers):
        self.udppublishers = udppublishers
    def get(self):
        for k,v in self.udppublishers.items():
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
        self.set_status(200)
        self.set_header('Cache-Control', 'no-cache')
        self.set_header('Allow', 'GET')
        self.set_header("Access-Control-Allow-Origin","*")
        self.set_header("Access-Control-Allow-Methods","HEAD, GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers","Content-Type")
    @gen.coroutine
    def get(self):
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
            #print ("mjpeg",now-ta,ta)
            self.write(("\r\n--BOUNDARY\r\nContent-Type: image/jpeg\r\nX-TimeDelta:%f\r\nLast-Modified: %s\r\nX-TimeStamp: %f\r\nContent-Length: %d\r\n\r\n" % (now-ta,DT.datetime.utcfromtimestamp(ta).isoformat(),ta,len(ta_message[1]))).encode("ascii"))
            yield self.write(ta_message[1])
            yield self.flush()
        self.write(b"\r\n--BOUNDARY\r\n\r\n")
        yield self.flush()


def makeffmpeg_screen(input,parts,listeners,rtp,jpeg,rtpopts,jpegopts,inputrate,gop=120,rtpbitrate=None,jpegquality=None,rtpcodec="h264",jpegcodec=None,rtpquality=None):
    nocrop = False
    args = ["ffmpeg"]
    if inputrate != 0:
        args.append("-r")
        args.append(inputrate)
    if input == "screen":
        if sys.platform.startswith("win"):
            if False and len(parts) == 1: # optimize
                pa = ("-offset_x %d -offset_y %d -video_size %dx%d" % (parts[0],parts[1],parts[2],parts[3])).split(" ")
                nocrop = True
            else:
                pa = []
            command  = ['-f','gdigrab'] + pa + ['-i','desktop']
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
                args.append("-vcodec")
                args.append(rtpcodec)
                if rtpquality is not None:
                    args.append("-q:v")
                    args.append("%d" % rtpquality)
                if rtpbitrate is not None:
                    args.append("-b:v")
                    args.append(rtpbitrate)
                if gop != 0:
                    args.extend(["-g","%d" %gop])
                if rtpcodec.startswith("h264"):
                    args.extend("-bf 0 -preset ultrafast -tune zerolatency".split(" "))
                    if rtpcodec == "h264" or rtpcodec == "libx264":
                        args.extend("-x264-params scenecut=0".split(" "))
                elif rtpcodec.startswith("mpeg4"):
                    args.append("-bf")
                    args.append("0")
                args.append("-f")
                args.append("rtp")
                args.append("-sdp_file")
                args.append(listeners[i]["sdp"])
                if rtpopts != "":
                    args.append(rtpopts)
                #JPEG for RTP -> vsample bug if using -f rtp
                #
                #http://www.ingerop.fr/sites/all/libraries/ffmpeg/libavformat/rtpenc_jpeg.c
                #https://trac.ffmpeg.org/ticket/4709
                #
                # RFC 2435 requires 4:2:2 mjpeg over rtp to be encoded with vsample[] 1,1,1 not 2,2,2
                # https://tools.ietf.org/html/rfc2435
                args.append("rtp://127.0.0.1:%d" % listeners[i]["rtp"])
            if jpeg:
                args.append("-map")
                args.append("[out%dB]"% i)
                args.append("-f")
                args.append("mjpeg")
                if jpegcodec is not None:
                    args.append("-vcodec")
                    args.append("%s" % jpegcodec)                    
                if jpegquality is not None:
                    args.append("-q:v")
                    args.append("%d" % jpegquality)
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
    parser.add_argument('--verbose', type=str2bool, nargs='?',
                            const=True, default=False,help="verbose")
    parser.add_argument('--gop', default=120,type=int,help="grouping for keyframe")
    parser.add_argument('--hwaccel', type=str2bool, nargs='?',
                            const=True, default=False,help="hwaccel for Intel QSV")
    parser.add_argument('--rtpcodec',default="mpeg4",help="rtp codec, e.g. mpeg4 or h264, note that mjpeg is not supported")
    parser.add_argument('--rtpbitrate',default=None,help="bitrate e.g. 300k")
    parser.add_argument('--jpeg', type=str2bool, nargs='?',
                            const=True, default=True,help="enables JPEG (default on)")
    parser.add_argument('--jpegquality', help="quality 2-31 passed -q:v",default=None,type=int)
    parser.add_argument('--rtpquality', help="quality 2-31 passed -q:v",default=None,type=int)
    parser.add_argument('--jpegopts',help="extra mjpeg options for ffmpeg: use ffmpeg -h encoder=mjpeg",default="")
    parser.add_argument('--rtpopts',help="extra rtp options for ffmpeg: use ffmpeg -h encoder=mpeg4 or h264",default="")
    parser.add_argument('--inputrate',help="grabbing rate (Hz) as -r to be put for the input (default 0)",default=0,type=int)
    parser.add_argument('--input',help="input value. Use 'screen' for desktop otherwise the ffmpeg input comprising the -i (default screen)",default="screen")
    parser.add_argument('--preferhttp',type=str2bool, nargs='?',
                            const=True, default=True,help="prefer http for internal connection with ffmpeg")
    parser.add_argument('--quittable',type=str2bool, nargs='?',
                            const=True, default=True,help="allow quitting")
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
            hserver = UDPServerToCoroutine(holder.onrtp)
            hserver.bind(0,family=socket.AF_INET)
            hserver.start()
            listeners[i]["rtp"] = hserver.ports()[0]
            listeners[i]["sdp"] = sdpfilename
            # start and describe rtp like in RTSP
            handlers.append((r"/rtp%d/([^/]+)/(\d+)" % i, RTPHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
            handlers.append((r"/sdp%d/([^/]+)/(\d+)" % i, SDPHandlerCustom, dict(filename=sdpfilename)))
            handlers.append((r"/sdp%d" % i, SDPHandler, dict(filename=sdpfilename)))
            handlers.append((r"/rtp%d" % i, RTPChunkedHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
            handlers.append((r"/rtpws%d" % i, RTPWSHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
            handlers.append((r"/rtpdump%d" % i, RTPDumpHandler, dict(hub=hub,name=("area%d"%i,"rtp"),udppublishers=udppublishers)))
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
    if args.quittable:
        handlers.append(("/quit",QuitHandler))

    print ("preparing  ffmpegg")
    print (listeners)
    syntax = makeffmpeg_screen(args.input,parts,listeners,args.rtp,args.jpeg,args.rtpopts,args.jpegopts,args.inputrate,rtpbitrate=args.rtpbitrate,rtpcodec="h264_qsv" if (args.rtpcodec == "h264" and args.hwaccel) else args.rtpcodec,rtpquality=args.rtpquality,jpegquality=args.jpegquality,jpegcodec="mjpeg_qs" if args.hwaccel else None)

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
    if args.rtsp != 0:
        rserver = RTSPServer(hub,udppublishers,"%d-%d" % (args.rtsp,args.rtsp+1))
        rserver.listen(args.rtsp)
        cserver = RTCPServer(rserver)
        cserver.listen(args.rtsp+1)
    signal.signal(signal.SIGINT, lambda x, y: IOLoop.instance().stop())
    IOLoop.instance().spawn_callback(lambda: startffmpeg(syntax,args.verbose))
    print ("loop starting")
    IOLoop.instance().start()

if __name__ == '__main__':
    main()

#ffplay <(curl http://127.0.0.1:8080/sdp0/127.0.0.1/1234)
#ffplay -protocol_whitelist rtp,file,udp <(curl http://127.0.0.1:8080/sdp0/127.0.0.1/1234)
#https://github.com/Akagi201/curl-rtsp

# Test RTSP
# OPTIONS rtsp://127.0.0.1:8666/ RTSP/1.0