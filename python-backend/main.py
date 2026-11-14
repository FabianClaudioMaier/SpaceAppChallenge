import astropy
import astroquery
import math
import numpy as np


from astroquery.ipac.irsa import Irsa

from astropy import units as u
from astropy import coordinates as coord

from astropy.cosmology import WMAP7
from astropy.coordinates import SkyCoord
from astropy.visualization import ZScaleInterval
from astropy.table import Table
from astropy.wcs import WCS
from astropy.io import fits


with fits.open(cutout_url(...)) as hdul:
    data = hdul[1].data                         # check hdul.info(); SPHEREx files have several HDUs
vmin, vmax = ZScaleInterval().get_limits(data)  # -> matplotlib.pyplot.imsave(buf, data, vmin=vmin, vmax=vmax, cmap="inferno", origin="lower")


def table_to_records(t):
    out = []
    for row in t:
        rec = {}
        for name in t.colnames:
            v = row[name]
            if np.ma.is_masked(v):
                v = None
            elif isinstance(v, np.generic):
                v = v.item()
            if isinstance(v, float) and math.isnan(v):
                v = None
            rec[name] = v
        out.append(rec)
    return out


def InputMiddleware():      # signed int probably (DEC and RA)
    pass

def FetchData():            # get data from Gaia ESA stuff
    pass

def GetImages():            # Extract the images i guess
    pass

def PushMiddleware():       # Output to Middleware
    pass

