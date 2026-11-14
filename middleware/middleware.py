from PIL import Image
from datetime import datetime
from io import BytesIO
import os
import time
import uuid


def sendToBackend(DEC, RA):
    '''
    sends coordinates DEC and RA to the backend

    parameters:
    DEC: Declination coordinate in degrees
    RA: Right Ascension coordinate in degrees

    output: None
    '''
    pass


def sendToFrontend(Video):
    '''
    saves the video in gif format to the video_cache folder and returns the video_id

    Parameters:
    Video: video in gif format

    output: video_id of the video
    '''
    video_id = str(uuid.uuid4())
    video_path = os.path.join("video_cache", f"{video_id}.gif")
    with open(video_path, "wb") as f:
        f.write(Video)
    return video_id


def convertToVideo(Images, Timestamps):
    '''
    converts the images and timestamps to a video format
    
    Parameters:
    Images: list of images in jpg format
    Timestamps: list of timestamps corresponding to the images in UTC format

    output: GIF image as bytes
    '''

    if len(Images) != len(Timestamps):
        raise ValueError("Images and Timestamps must have the same length")
    if not Images:
        raise ValueError("At least one image is required to create a GIF")

    image_timestamps = []
    for i, (image, timestamp) in enumerate(zip(Images, Timestamps)):
        if not isinstance(image, str) or not image.lower().endswith('.jpg'):
            raise ValueError(f"Image at index {i} is not a valid jpg file")
        if not isinstance(timestamp, str):
            raise ValueError(f"Timestamp at index {i} is not a valid string")
        try:
            parsed_timestamp = datetime.strptime(timestamp, '%Y-%m-%d %H:%M:%S')
        except ValueError as exc:
            raise ValueError(
                f"Timestamp at index {i} must use YYYY-MM-DD HH:MM:SS format"
            ) from exc
        image_timestamps.append((parsed_timestamp, image))

    image_timestamps.sort(key=lambda item: item[0])
    frames = []
    for _, image_path in image_timestamps:
        with Image.open(image_path) as image:
            frames.append(image.convert('RGB'))

    output = BytesIO()
    frames[0].save(
        output,
        format='GIF',
        save_all=True,
        append_images=frames[1:],
        duration=500,
        loop=0,
    )
    return output.getvalue()


CACHE_FOLDER = "video_cache"
MAX_AGE = 30 * 60  # 30 minutes


def clean_cache():

    current_time = time.time()

    for filename in os.listdir(CACHE_FOLDER):

        filepath = os.path.join(CACHE_FOLDER, filename)

        if not os.path.isfile(filepath):
            continue

        file_age = current_time - os.path.getmtime(filepath)

        if file_age > MAX_AGE:
            os.remove(filepath)
            print(f"Deleted old video: {filename}")
