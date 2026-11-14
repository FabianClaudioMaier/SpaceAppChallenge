



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
    sends video to the frontend

    Parameters:
    Video: video in gif format

    output: None
    '''
    pass


def convertToVideo(Images, Timestamps):
    '''
    converts the images and timestamps to a video format
    
    Parameters:
    Images: list of images in jpg format
    Timestamps: list of timestamps corresponding to the images in UTC format

    output: Video in gif format
    '''
    Video = None
    if len(Images) != len(Timestamps):
        raise ValueError("Images and Timestamps must have the same length")

    for i in range(len(Images)):
        if not isinstance(Images[i], str) or not Images[i].endswith('.jpg'):
            raise ValueError(f"Image at index {i} is not a valid jpg file")
        if not isinstance(Timestamps[i], str):
            raise ValueError(f"Timestamp at index {i} is not a valid string")
        Video = f"Video created from {len(Images)} images and {len(Timestamps)} timestamps"
        
    return Video

def testenvironment():
    pass


testenvironment()