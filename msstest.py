import mss
import subprocess
from ffmpy import FFmpeg
from pipe_non_blocking import pipe_non_blocking_set
import time
from asyncreader import *

def consumeout(reader,of):
	while not reader.empty():
		x = reader.read()
		print "got",len(x)
		of.write(x)

def main():
	n = 0
	of = open("out.mp4","wb")
	ff = None

	with mss.mss() as sct:
		while True:
			n = n + 1
			if n > 20:
				break
			t = time.time()
			sct_img = sct.grab(sct.monitors[0])
			dt = time.time()-t
			s = sct_img.size
			data = sct_img.rgb
			if ff is None:
				ff = FFmpeg(
					inputs={'pipe:0': "-f rawvideo -pix_fmt rgb24 -s:v %dx%d" % s},
					outputs={'pipe:1': '-pix_fmt yuv420p -c:v h264 -f ismv'}
				)
				print ff.cmd
				ff.runlive(stdout=subprocess.PIPE)
				print ff.process.stdout.fileno()
				reader = AsynchronousFileReader(ff.process.stdout)
				#pipe_non_blocking_set(ff.process.stdout.fileno())
				print "setup"
			print "frame",dt
			ff.process.stdin.write(data)
			consumeout(reader,of)
	ff.process.stdin.close() 
	while ff.process.poll():
		consumeout(reader,of)
	consumeout(reader,of)

if __name__ == '__main__':
	main()