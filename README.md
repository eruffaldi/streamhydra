# streamhydra


# Latency Tests

H264+RTP
ffmpeg -r 30 -f avfoundation -i 0 -vcodec h264 -f rtp -sdp_file x.sdp -maxrate 750k -bufsize 3000k -preset ultrafast -tune zerolatency -g 120 -x264opts no-scenecut  rtp://127.0.0.1:1234

ffplay x.sdp -protocol_whitelist rtp,file,udp -probesize 32 -sync ext  -fflags nobuffer

MPEG4+RTP
ffmpeg -r 30 -f avfoundation -i 0 -vcodec mpeg4 -bf 0 -g 60 -f rtp -sdp_file x.sdp rtp://127.0.0.1:1234

ffplay x.sdp -protocol_whitelist rtp,file,udp -probesize 32 -sync ext  -fflags nobuffer

MPEG+RTP bufsize
ffmpeg -r 30 -f avfoundation -i 0 -vcodec mpeg4 -bf 0 -b:v 1M -g 60 -f rtp -sdp_file x.sdp -maxrate 2M -bufsize 1M rtp://127.0.0.1:1234

