#!/usr/bin/env python
#TEST 
#   curl http://127.0.0.1:8766 | python mp4streamtest.py  -
#LIVE
#   ffplay http://127.0.0.1:8766
#Web
#   http://download.tsi.telecom-paristech.fr/gpac/mp4box.js/
#
#https://blog.carlmjohnson.net/post/on-using-go-channels-like-python-generators/
#
#Example: 

import subprocess # for piping
from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
import struct,os
import select
from Queue import Queue
from threading import Thread
from SocketServer import ThreadingMixIn
import socket

ATOM_HEADER = {
    # Mandatory big-endian unsigned long followed by 4 character string
    #                      (   size    )             (      type      )
    'basic': '>L4s',
    # Optional big-endian long long
    #          (    64bit size    )
    # Only used if basic size == 1
    'large': '>L4sQ',
}

def pickffmpegsource(what):
    if what == "screen":
        command = 'ffmpeg -f avfoundation -i "capture Screen 0"'
    elif what == "webcam":
        command = 'ffmpeg -r 30 -s 640x480 -f avfoundation -i "FaceTime HD Camera" ' 
    else:
        command = 'ffmpeg -stream_loop 1  -i \"%s\"' % what 
    return command
source = None

def waitsocket(s,timeout):
    socket_list = [s]
    read_sockets, write_sockets, error_sockets = select.select(socket_list , [], socket_list,timeout)
    if len(error_sockets) != 0:
        print "waitsocket fail"
        return False
    else:
        return True

class Source:
    def __init__(self,what,how,hostport=None,hostsocket=None):
        self.what = what
        self.headwait = set()
        self.moofwait = set()
        self.live = set()
        self.ready = False
        self.prebuf = ""
        self.done = False
        self.hostport = hostport
        self.hostsocket = hostsocket

        self.queue = Queue()
        if how == "mp4":
            worker = Thread(target=self.runmp4)
        else:
            worker = Thread(target=self.runjpeg)
        worker.setDaemon(True)
        worker.start()
    def addlistener(self,l):
        self.queue.put(("add",l))
    def dellistener(self,l):
        self.queue.put(("del",l))
    def runmp4(self):
        DataChunkSize = 10000

        #See this http://fomori.org/blog/?p=1213
        # for tuning more
        #-x264opts crf=20:vbv-maxrate=3000:vbv-bufsize=100:intra-refresh=1:slice-max-size=1500:keyint=30:ref=1
        if self.hostsocket is None:
            FNULL = open(os.devnull, 'w')

            #(echo "--video boundary--"; raspivid -w 1920 -h 1080 -fps 30 -pf high -n -t 0 -o -;)
            #2command = 'ffmpeg -r 30 -s 640x480 -f avfoundation -i  "FaceTime HD Camera" -f h264 - | gst-launch-1.0 -e -q fdsrc fd=0 ! video/x-h264,width=640,height=480,framerate=30/1,stream-format=byte-stream ! h264parse ! mp4mux streamable=true fragment-duration=10 presentation-time=true ! filesink location=/dev/stdout'
            #-movflags isml+frag_keyframe
            suffix= "  -nostdin -g 1 -preset ultrafast -vcodec libx264 -tune zerolatency -b:v 5000k -frag_duration 100 -f ismv - -f mjpeg tcp://127.0.0.1:%d" % self.hostport
            command = pickffmpegsource(self.what)
            command += suffix
            print("mp4: running stdin:\n %s" % (command, ))
            subproc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=FNULL,bufsize=-1, shell=True)
            subprocfx = (lambda: subproc.poll() is None,lambda n: subproc.stdout.read(n),subproc.kill)
        else:
            (clientsocket, address) = self.hostsocket.accept()
            clientsocket.setblocking(0)
            print "mp4 listening as",address
            self.hostsocket.close()
            subprocfx = (lambda: waitsocket(clientsocket,200),lambda n: clientsocket.read(n),lambda: clientsocket.close())

        print("mp4: starting polling loop.")
        ss = StreamingAtom(False)
        while subprocfx[0]():
            try:
                msg = self.queue.get(False)
            except:
                msg = None
            if msg is not None:
                print "MAIN msg",msg
                if msg[0] == "add":
                    l = msg[1]
                    if self.ready:
                        print "MAIN adding listener in MOOF mode using prefix",len(self.prebuf),l.id
                        l.enqueue(self.prebuf)
                        self.moofwait.add(l)
                    else:
                        print "MAIN adding listener in HEAD mode",l.id
                        self.headwait.add(l)
                        # wait for foll HEAD
                elif msg[0] == "del":
                    l = msg[1]
                    print "MAIN removing listener",l.id
                    if l in self.headwait:
                        self.headwait.remove(l)
                    elif l in self.moofwait:
                        self.moofwait.remove(l)
                    elif l in self.live:
                        self.live.remove(l)

            try:
                stdoutdata = subprocfx[1](DataChunkSize)
            except:
                break
            # append to p arser
            ss.data += stdoutdata
            #print "MAIN read",len(stdoutdata)
            prebuf = self.prebuf
            for a,t,s in ss.ondata():
                #print "\tMAIN part",t,a,len(s) if type(s) is str else s
                if not self.ready:
                    if a == StreamingAtom.BEGIN and t == "moof":
                        # unlock HEAD and MOOF pending
                        self.ready = True
                        print "\t\tMAIN found head",len(prebuf),"with pending",len(self.headwait),len(self.moofwait)
                        if len(self.headwait) > 0:
                            for l in self.headwait:
                                l.enqueue(self.prebuf)
                            self.live |= self.headwait
                            self.headwait.clear()
                        if len(self.moofwait) > 0:
                            self.live |= self.moofwait
                            self.moofwait.clear()
                    elif a == StreamingAtom.BODY:
                        # accumulate
                        prebuf += s                
                else:
                    if len(self.moofwait) > 0 and a == StreamingAtom.BEGIN and t == "moof":
                        self.live |= self.moofwait
                        self.moofwait.clear()
                    elif a == StreamingAtom.BODY:
                        for l in self.live:
                            l.enqueue(s)
                    
            self.prebuf = prebuf
        print "MAIN subprocess mp4 exit"
        for x in self.headwait:
            try:
                x.q.put("")
            except:
                pass
        for x in self.moofwait:
            try:
                x.q.put("")
            except:
                pass
        for x in self.live:
            try:
                x.q.put("")
            except:
                pass
        try:
            subprocfx[2]()
        except:
            pass   
        self.done = True
    def runjpeg(self):
        DataChunkSize = 10000

        if self.hostsocket is None:
            FNULL = open(os.devnull, 'w')

            #(echo "--video boundary--"; raspivid -w 1920 -h 1080 -fps 30 -pf high -n -t 0 -o -;)
            #2command = 'ffmpeg -r 30 -s 640x480 -f avfoundation -i  "FaceTime HD Camera" -f h264 - | gst-launch-1.0 -e -q fdsrc fd=0 ! video/x-h264,width=640,height=480,framerate=30/1,stream-format=byte-stream ! h264parse ! mp4mux streamable=true fragment-duration=10 presentation-time=true ! filesink location=/dev/stdout'
            suffix= " -nostdin -f mjpeg -q:v 3 -huffman optimal -"
            command = pickffmpegsource(self.what)
            command += suffix
            print("mjpeg running stdin: %s" % (command, ))
            subproc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=FNULL ,bufsize=-1, shell=True)            
            subprocfx = (lambda : subproc.poll() is None,lambda n: subproc.stdout.read(n),subproc.kill)
        else:
            print "mjpeg accepting ",self.hostport
            (clientsocket, address) = self.hostsocket.accept()
            clientsocket.setblocking(0)
            print "mjpeg accepted ",address
            self.hostsocket.close()
            subprocfx = (lambda: waitsocket(clientsocket,200),lambda n: clientsocket.recv(n),lambda: clientsocket.close())

        ss = StreamingJpeg()
        print("jpeg starting polling loop.")
        while subprocfx[0]():
            try:
                msg = self.queue.get(False)
            except:
                msg = None
            if msg is not None:
                print "MAIN msg",msg
                if msg[0] == "add":
                    l = msg[1]
                    self.live.add(l)
                elif msg[0] == "del":
                    l = msg[1]
                    if l in self.live:
                        self.live.remove(l)
            try:
                stdoutdata= subprocfx[1](DataChunkSize) 
            except:
                print "jpg error"
                break
            if len(stdoutdata) != 0:
                # append to p arser
                ss.data += stdoutdata
                for s in ss.ondata():
                    for l in self.live:
                        l.enqueue(s)                    
        #print "MAIN subprocess exit",subproc.returncode
        for x in self.live:
            try:
                x.q.put("")
            except:
                pass
        try:
            subprocfx[2]()
        except:
            pass


class StreamingJpeg:
    def __init__(self):
        self.data = "" # buffer
        self.buffer = "" # for whole
    def ondata(self):
        data = self.data
        while len(data) != 0:
            k = data.find("\xFF\xD8")
            #print len(data),k,len(self.buffer)
            #print "jpeg",len(data),k
            if k < 0:
                self.buffer += data
                data = ""
                break
            else:
                self.buffer += data[0:k]
                if len(self.buffer) != 0:
                    yield self.buffer
                self.buffer = "\xFF\xD8"
                data = data[k+2:]
        self.data = data

class StreamingAtom:    
    END=0
    BEGIN=1
    BODY=2
    def __init__(self,whole):
        self.tag = None
        self.tagsize = 0
        self.left = 0
        self.data = "" # buffer
        self.buffer = "" # for whole
        self.whole= whole
    def ondata(self):
        data = self.data
        while len(data) != 0:
            if self.tag is not None:
                n = min(self.left,len(data))
                self.left -= n
                if self.left == 0:
                    if self.whole:
                        # emit one tag
                        self.buffer += data[0:n]
                        yield (StreamingAtom.BODY,self.tag,self.buffer)
                        self.buffer = ""
                    else:
                        # emit body and close
                        yield (StreamingAtom.BODY,self.tag,data[0:n])
                        yield (StreamingAtom.END,self.tag,None)
                    self.tag = None
                else:
                    if self.whole:
                        # append
                        self.buffer += data[0:n]
                    else:
                        # emit part
                        yield (StreamingAtom.BODY,self.tag,data[0:n])                        
                # in any case reduce data
                data = data[n:]
            elif len(data) < 8:
                # not enough for a header
                break
            else:
                s = struct.unpack(">L",data[0:4])[0]
                tag = data[4:8]
                if s == 1:
                    if len(data) < 16:
                        # not enough for the big header
                        break
                    else:
                        bn = 16
                        s = struct.unpack(">Q",data[8:16])[0]
                else:
                    bn = 8
                self.tag  = tag
                self.left = s-bn
                self.tagsize = s
                if not self.whole: 
                    yield (StreamingAtom.BEGIN,self.tag,s+bn) # begin gives full size for preallocation
                    yield (StreamingAtom.BODY,self.tag,data[0:bn]) # header => this will emit BODY for header and body for rst
                else:
                    self.buffer += data[0:bn] # append headr
                data = data[bn:]
        self.data = data

class RequestHandler(BaseHTTPRequestHandler):
    COUNT = 0
    def _writeheaders(self,n=None):
        RequestHandler.COUNT+=1
        self.id =         RequestHandler.COUNT
        if self.path == "/mp4":
            self.send_response(200) # 200 OK http response
            self.send_header('Content-type', 'video/mp4')
            # application/octet-stream
            self.send_header('Transfer-Encoding', 'chunked')
        elif self.path == "/jpeg":
            self.send_response(200) # 200 OK http response
            self.send_header('Content-type', 'image/jpeg')
            if n is not None:
                self.send_header('Content-Length', "%d" % n)
        elif self.path == "/mjpeg":
            self.send_response(200) # 200 OK http response
            self.send_header('Content-type', 'multipart/x-mixed-replace;boundary=BOUNDARY')
        else:
            self.send_response(404)
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.end_headers()
    def do_HEAD(self):
        self._writeheaders()

    def write_chunk(self,chunk):
        tosend = '%X\r\n'%(len(chunk))
        self.wfile.write(tosend)
        self.wfile.write(chunk)
        self.wfile.write("\r\n")

    def enqueue(self,chunk):
        # TODO size limiter
        self.q.put(chunk)
    def do_GET(self):
        print "HANDLER handling",self.path
        if self.path == "/mp4":
            print "HANDLER headers",self.headers
            self._writeheaders()

            self.q = Queue()
            print "HANDLER adding",self.id
            source.addlistener(self)
            while True:
                try:
                    a = self.q.get(True,0.5) # make waitable
                    if a == "": 
                        break
                except:
                    # empty
                    if source.done:
                        break
                    a = ""
                    print "HANDLER ping",self.id
                #print "HANDLER write",len(a)
                try:
                    self.write_chunk(a)
                except:
                    print "HANDLER network exception",self.id
                    break
            source.dellistener(self)
            print "HANDLER removed",self.id
        elif self.path == "/jpeg":
            print "HANDLER jpeg headers",self.headers
            self.q = Queue()
            sourceJ.addlistener(self)
            while True:
                try:
                    a = self.q.get(True,0.5) # make waitable
                    if a == "": 
                        break                        
                except:
                    if sourceJ.done:
                        print "source is done"
                        self.send_error(404)
                        break
                    else:
                        continue
                if len(a) != 0:
                    try:
                        self._writeheaders(len(a))
                        self.wfile.write(a)
                    except:
                        print "HANDLER network exception",self.id
                    print "HANDLER image deleivere drmoving"
                    break
            sourceJ.dellistener(self)
        elif self.path == "/mjpeg":
            print "HANDLER mjpeg headers",self.headers
            self._writeheaders()

            self.q = Queue()
            print "HANDLER adding",self.id
            sourceJ.addlistener(self)
            while True:
                try:
                    a = self.q.get(True,0.5) # make waitable
                    if a == "": 
                        break
                except:
                    a = ""
                    if sourceJ.done:
                        #Note that the encapsulation boundary must occur at the beginning of a line, i.e., following a CRLF, and that that initial CRLF is considered to be part of the encapsulation boundary rather than part of the preceding part. 
                        self.wfile.write("\r\n--BOUNDARY\r\n\r\n")
                        break
                    else:
                        continue
                #print "HANDLER write",len(a)
                if len(a) != 0:
                    try:
                        #Note that the encapsulation boundary must occur at the beginning of a line, i.e., following a CRLF, and that that initial CRLF is considered to be part of the encapsulation boundary rather than part of the preceding part. 
                        self.wfile.write("\r\n--BOUNDARY\r\n"+"Content-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(a))
                        self.wfile.write(a)
                    except:
                        print "HANDLER network exception",self.id
                        break
            sourceJ.dellistener(self)
            print "HANDLER removed",self.id
        else:
            self.send_error(404)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='server')
    parser.add_argument('--test')
    parser.add_argument('--source',help="can be filename or: webcam, screen",default="webcam")

    args = parser.parse_args()

    if args.test is not None:
        aa = sys.stdin if args.text == "-" else open(args.text,"rb")
        prebuf = ""
        ss = StreamingAtom(False)
        ready = False
        t2t = dict()
        t2t[StreamingAtom.BEGIN] = "begin"
        t2t[StreamingAtom.END] = "end"
        t2t[StreamingAtom.BODY] = "body"
        while True:
            stdoutdata = aa.read(10000)
            if len(stdoutdata) == 0:
                break
            ss.data += stdoutdata
            for a,t,s in ss.ondata():
                print "event",t,t2t[a],len(s) if type(s) is str else s
                if not ready and a == StreamingAtom.BEGIN and t == "moof":
                    ready = True
                    print "\t found head",len(prebuf)
                elif a == StreamingAtom.BODY:
                    prebuf += s        
    else:

        serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        serversocket.bind(("0.0.0.0", 0))
        #socket.setblocking(0)
        serversocket.listen(5)
        r = serversocket.getsockname()

        srvr = ThreadedHTTPServer( ('', 8766), RequestHandler)

        sourceJ = Source(args.source,"mjpeg",hostsocket=serversocket,hostport=r[1])
        source = Source(args.source,"mp4",hostport=r[1])

        srvr.serve_forever()
        sys.exit(0)
