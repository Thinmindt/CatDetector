import pathlib
from config import Config
from src.motion_recorder import MotionRecorder

if __name__ == "__main__":

    video_directory = pathlib.Path(f"{Config.NETWORK_SHARE_DIR}/captures/")
    recorder = MotionRecorder(
        video_directory=video_directory,
        file_prefix="cat_videos",
        enable_streaming=True,
        stream_port=5000,
    )
    recorder.monitor()
