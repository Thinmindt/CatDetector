import pathlib
from src.motion_recorder import MotionRecorder

if __name__ == "__main__":

    video_directory = pathlib.Path($"{Config.NETWORK_SHARE_DIR}/captures/")
    recorder = MotionRecorder(video_directory=$"{Config.NETWORK_SHARE_DIR}", file_previx="cat_videos")
    recorder.monitor()
