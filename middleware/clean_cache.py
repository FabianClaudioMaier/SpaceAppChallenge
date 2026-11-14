import time
import os


def clean_cache(CACHE_FOLDER="video_cache", MAX_AGE=30 * 60):

    '''
    Cleans the video cache folder by deleting files older than MAX_AGE seconds.

    Parameters:
    CACHE_FOLDER: The folder where cached videos are stored (default: "video_cache")
    MAX_AGE: The maximum age of files in seconds before they are deleted (default: 30 minutes)
    
    Output: None
    '''

    current_time = time.time()

    for filename in os.listdir(CACHE_FOLDER):

        filepath = os.path.join(CACHE_FOLDER, filename)

        if not os.path.isfile(filepath):
            continue

        file_age = current_time - os.path.getmtime(filepath)

        if file_age > MAX_AGE:
            os.remove(filepath)
    pass

while True:
    clean_cache()
    time.sleep(60)  # Check every minute