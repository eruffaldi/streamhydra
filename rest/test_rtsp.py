# ffmpeg -i X -f mp4 -sdp_file O.mp4.sdp O.mp4
# rate adaprtation or interleaved not supported
from rtsp.server import RTSPServer, BaseRTSPRequestHandler
from socketserver import ThreadingMixIn
import os

class ThreadedRTSPServer(ThreadingMixIn, RTSPServer):
    """Handle requests in a separate thread."""
    pass

class RequestHandler(BaseRTSPRequestHandler):
    sessionid = 1234
    def do_unsupported(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        self.end_headers();
    def do_OPTIONS(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        self.send_response(200)
        self.send_header("CSeq",self.headers["CSeq"])
        self.send_header("Public","DESCRIBE, SETUP, TEARDOWN, PLAY")
        self.end_headers()
    def send_redirect(self,url,cseq):
        #REDIRECT rtsp://example.com/media.mp4 RTSP/1.0
        self.send_redirect_only(self.path)
        self.send_header("CSeq",cseq)
        self.send_header("Location",url)
        self.end_headers()
        #CSeq: 11
        #Location: rtsp://bigserver.com:8001
        #Range: clock=19960213T143205Z

        pass
    def do_PLAY(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        self.send_error(202)

        #Req
        #Range: npt=5-20
        #Session: 12345678

        #Out
        #RTP-Info: url=rtsp://example.com/media.mp4/streamid=0;seq=9810092;rtptime=3450012


    def do_PAUSE(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        self.send_error(202)

    def do_SETUP(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        try:
            self.send_response(200)
            self.send_header("CSeq",self.headers["CSeq"])
            self.send_header("Transport",self.headers["Transport"]+";ssrc=X;server_port=2000-2001")
            self.send_header("Session",str(RequestHandler.sessionid))
            RequestHandler.sessionid += 1
            self.end_headers()
        except Exception as e:
            print (e)

        #Out
        #Transport: RTP/AVP;unicast;client_port=8000-8001;server_port=9000-9001;ssrc=1234ABCD

        #Session: 12345678
        #ssrc:
        #  The ssrc parameter indicates the RTP SSRC [24, Sec. 3] value
        #  that should be (request) or will be (response) used by the
        #  media server. This parameter is only valid for unicast
        #  transmission. It identifies the synchronization source to be
        #  associated with the media stream.

    def do_DESCRIBE(self):
        print ("Method:",self.command)
        print ("Path:",self.path)
        print ("Headers--\n",self.headers,"\n--")
        if self.headers.get("Accept") == "application/sdp":
            if os.path.isfile(self.path[1:]+".sdp"):
                data = open(self.path[1:]+".sdp","rb").read()
                self.send_response(200)
                self.send_header("CSeq",self.headers["CSeq"])
                self.send_header("Content-Type","application/sdp")
                self.send_header("Content-Base","")
                self.send_header("Content-Length",len(data))
                self.end_headers()
            else:
                self.send_error(404)
        else:
            self.send_error(303)

def main():
    # rtsp://127.0.0.1:9001/mp4.sdp
    srvr = ThreadedRTSPServer( ('', 9001), RequestHandler)
    srvr.serve_forever()
    sys.exit(0)


if __name__ == '__main__':
    main()
