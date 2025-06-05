import cv2
import threading
from flask import Flask, Response, render_template_string
from picamera2 import Picamera2
import time


class WebStreamer:
    def __init__(self, port=5000):
        self.app = Flask(__name__)
        self.picam2 = Picamera2()
        self.picam2.configure(
            self.picam2.create_video_configuration(main={"size": (640, 480)})
        )
        self.picam2.start()
        self.port = port
        self.setup_routes()

    def setup_routes(self):
        @self.app.route("/")
        def index():
            return render_template_string(
                """
            <!DOCTYPE html>
            <html>
            <head><title>Cat Detector Live Feed</title></head>
            <body>
                <h1>Cat Detector Live Feed</h1>
                <img src="{{ url_for('video_feed') }}" style="width:100%; max-width:800px;">
            </body>
            </html>
            """
            )

        @self.app.route("/video_feed")
        def video_feed():
            return Response(
                self.generate_frames(),
                mimetype="multipart/x-mixed-replace; boundary=frame",
            )

    def generate_frames(self):
        while True:
            try:
                # Capture frame
                frame = self.picam2.capture_array()

                # Convert to JPEG
                ret, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70]
                )
                frame_bytes = buffer.tobytes()

                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                )

                time.sleep(0.033)  # ~30 FPS
            except Exception as e:
                print(f"Streaming error: {e}")
                break

    def start(self):
        self.app.run(host="0.0.0.0", port=self.port, debug=False, threaded=True)
