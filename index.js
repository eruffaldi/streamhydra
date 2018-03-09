/*globals window, console, XMLHttpRequest, document, Uint8Array, DOMParser, URL*/

(function () { /* code */

    'use strict';

    // Global Parameters from .mpd file
    var file;  // MP4 file
    var width;  //  Native width and height
    var height;

    // Elements
    var videoElement = document.getElementById('myVideo');
    var playButton = document.getElementById("load");
    //videoElement.poster = "poster.png";

    // Description of initialization segment, and approx segment lengths
    var initialization;

    // Video parameters
    var bandwidth; // bitrate of video

    // Parameters to drive segment loop
    var index = 0; // Segment to get
    var segments;

    // Source and buffers
    var mediaSource;
    var videoSource;

    // Parameters to drive fetch loop
    var segCheck;
    var lastTime = 0;
    var bufferUpdated = false;

    // Flags to keep things going
    var lastMpd = "";
    var requestId = 0;

    //  Logs messages to the console
    function log(s) {
        //  send to console
        //    you can also substitute UI here
        console.log(s);
    }

    //  Clears the log
    function clearLog() {
        console.clear();
    }

    function timeToDownload(range) {
        var vidDur = range.split("-");
        // Time = size * 8 / bitrate
        return (((vidDur[1] - vidDur[0]) * 8) / bandwidth);
    }


    function updateFunct() {
        //  This is a one shot function, when init segment finishes loading,
        //    update the buffer flag, call getStarted, and then remove this event.
        bufferUpdated = true;
        //  Now that video has started, remove the event listener
        //videoSource.removeEventListener("update", updateFunct);
    }

    //  Load video's initialization segment
    function initVideo(url) {
        var xhr = new XMLHttpRequest();
        if (url) { // make sure we've got incoming params
            console.log("Loading ",url)
            // Set the desired range of bytes we want from the mp4 video file
            xhr.open('GET', url);
            //xhr.setRequestHeader('Transfer-Encoding', 'chunked');

            //xhr.setRequestHeader("Range", "bytes=" + range);
            //segCheck = (timeToDownload(range) * 0.8).toFixed(3); // use point eight as fudge factor
            xhr.send();
            xhr.responseType = 'moz-chunked-arraybuffer';
            try {
                xhr.addEventListener("progress", function () {
                    console.log("progress")
                        // Add response to buffer
                        try {
                             console.log("Slice ",url,xhr.response)
                            var q = new Uint8Array(xhr.response)
                           videoSource.appendBuffer(q);
                            // Wait for the update complete event before continuing
                            //videoSource.addEventListener("update", updateFunct, false);

                        } catch (e) {
                            log('Exception while appending content', e);
                        }
                }, false);
            } catch (e) {
                log(e);
            }
        } else {
            return; // No value for range or url
        }
    }

    // Create mediaSource and initialize video
    function setupVideo() {
        clearLog(); // Clear console log

        //  Create the media source
        if (window.MediaSource) {
            mediaSource = new window.MediaSource();
        } else {
            log("mediasource or syntax not supported");
            return;
        }
        var url = URL.createObjectURL(mediaSource);
        videoElement.pause();
        videoElement.src = url;
        videoElement.width = 640;
        videoElement.height = 480;

        // Wait for event that tells us that our media source object is
        //   ready for a buffer to be added.
        mediaSource.addEventListener('sourceopen', function (e) {
            try {
                videoSource = mediaSource.addSourceBuffer('video/mp4; codecs="avc1.42E01E"');
                //'video/mp4 codecs="avc1.42c00d"');
                initVideo(lastMpd);
            } catch (ex) {
                log('Exception calling addSourceBuffer for video' +ex);
                return;
            }
        }, false);

        // Handler to switch button text to Play
        videoElement.addEventListener("pause", function () {
            playButton.innerText = "Play";
        }, false);

        // Handler to switch button text to pause
        videoElement.addEventListener("playing", function () {
            playButton.innerText = "Pause";
        }, false);
    }

    // Click event handler for load button
    playButton.addEventListener("click", function () {
        //  If video is paused then check for file change
        if (videoElement.paused === true) {
            // Retrieve mpd file, and set up video
            var curMpd = document.getElementById("filename").value;
            //  If current mpd file is different then last mpd file, load it.
            if (curMpd !== lastMpd) {
                //  Cancel display of current video position
                window.cancelAnimationFrame(requestId);
                console.log("adding ",curMpd)
                lastMpd = curMpd;
                setupVideo();
            } else {
                console.log("same ",curMpd)
                //  No change, just play
                videoElement.play();
            }
        } else {
            //  Video was playing, now pause it
            videoElement.pause();
        }
    }, false);

    // Do a little trickery, start video when you click the video element
    videoElement.addEventListener("click", function () {
        playButton.click();
    }, false);

    // Event handler for the video element errors
    document.getElementById("myVideo").addEventListener("error", function (e) {
        log("video error: " + e.message);
    }, false);

}());