import warnings
from typing import List

import numpy as np
import xarray as xr
import rasterio as rst

from affine import Affine
from rasterio.transform import guard_transform
from rasterio.enums import MaskFlags
from rasterio.transform import array_bounds


class NodataWarning(UserWarning):
    pass

# *should* limit NodataWarnings to once, but doesn't! Bug in CPython.
# warnings.filterwarnings("once", category=NodataWarning)
# instead we resort to a global bool
already_warned_nodata = False
already_warned_crs = False


class Raster:
    def __init__(
        self, 
        raster, 
        transform : rst.Affine = None, 
        crs : str | rst.CRS = None,
        nodata : float = None, 
        bands : int | List = None,
    ):
        self.data = None
        self.src = None
        self.bands = bands
        self.count = len(bands) if isinstance(self.bands, list) else 1

        if isinstance(raster, np.ndarray):
            self.shape = raster.shape
            self.height = raster.shape[-2]
            self.width = raster.shape[-1]
            self.dtype = raster.dtype
            if len(self.shape) == 3:
                if bands is None:
                    self.data = raster[:]
                else:
                    self.data = raster[bands]
            else:
                self.data = raster[:]

            if transform is None:
                raise ValueError("Specify affine transform for numpy arrays")
            else:
                self.transform = transform
            self.res = self.transform[0]

            self.bounds = array_bounds(self.height, self.width, self.transform)
            self.crs = crs
            self.nodata = nodata

        elif isinstance(raster, xr.DataArray):
            self.shape = raster.shape
            self.height = raster.shape[-2]
            self.width = raster.shape[-1]
            self.dtype = raster.dtype
            if len(self.shape) == 3:
                if bands is None:
                    self.data = raster.data[:]
                else:
                    self.data = raster.loc[bands]
            else:
                self.data = raster.data[:]

            self.transform = raster.attrs['transform']
            self.res = self.transform[0]
            self.bounds = array_bounds(self.height, self.width, self.transform)
            self.crs = raster.attrs['crs']
            if nodata is not None:
                # override with specified nodata
                self.nodata = float(nodata)
            else:
                self.nodata = raster.attrs['nodata']

        else:
            self.src = rst.open(raster, 'r')
            self.transform = guard_transform(self.src.transform)
            self.res = self.transform[0]
            self.shape = self.src.shape
            self.height = self.src.height
            self.width = self.src.width
            self.bounds = self.src.bounds
            self.data = self.src.read(indexes=bands)
            self.crs = self.src.crs
            self.count = self.src.count
            self.dtype = self.src.profile['dtype']

            if nodata is not None:
                # override with specified nodata
                self.nodata = float(nodata)
            else:
                self.nodata = self.src.nodata
        
        
        self.meta = {
            'driver' : 'GTiff',
            'count' : self.count,
            'dtype' : self.dtype,
            'height' : self.height,
            'width' : self.width,
            'transform' : self.transform,
            'nodata' : self.nodata,
            'crs' : self.crs
        }
    
    def read(self, masked=False, bounds=None, window=None, boundless=True):
        bands = self.bands
        nodata = self.nodata
        crs = self.crs
        if crs is None:
            crs = 'EPSG:4326'
            global already_warned_crs
            if not already_warned_crs:
                warnings.warn(
                    "Setting crs to EPSG:4326; specify crs explicitly", NodataWarning
                )
                already_warned_crs = True

        if all([i is None for i in [bounds, window]]):
            if self.src and bands is not None:
                bands = [b-1 for b in bands] if isinstance(bands, list) else bands-1
            return Raster(self.data, self.transform, crs, nodata, bands)

        else:
            # new_affine = self.affine_transform(self, bounds, window, boundless)
            transform = self.affine_transform(self, bounds, window, boundless)

            if self.data is not None:
                # It's an ndarray already
                new_array = boundless_array(
                    self.data, window=window, nodata=nodata, masked=masked
                )
            elif self.src:
                # It's an open rasterio dataset
                if all(
                    MaskFlags.per_dataset in flags for flags in self.src.mask_flag_enums
                ):
                    if not masked:
                        masked = True
                        warnings.warn(
                            "Setting masked to True because dataset mask has been detected"
                        )

                new_array = self.src.read(
                    indexes=bands, window=window, boundless=boundless, masked=masked
                )
                if bands is not None:
                    bands = [b-1 for b in bands] if isinstance(bands, list) else bands-1

        return Raster(new_array, transform, crs, nodata, bands)
    
    def affine_transform(self, bounds=None, window=None, boundless=True):
        """Performs a read against the underlying array source

        Parameters
        ----------
        bounds: bounding box
            in w, s, e, n order, iterable, optional
        window: rasterio-style window, optional
            bounds OR window are required,
            specifying both or neither will raise exception
        masked: boolean
            return a masked numpy array, default: False
        boundless: boolean
            allow window/bounds that extend beyond the dataset’s extent, default: True
            partially or completely filled arrays will be returned as appropriate.

        Returns
        -------
        Raster object with update affine and array info
        """
        # Calculate the window
        if bounds and window:
            raise ValueError("Specify either bounds or window")
        if bounds:
            win = bounds_window(bounds, self.transform)
        elif window:
            win = window
        else:
            raise ValueError("Specify either bounds or window")

        if not boundless and beyond_extent(win, self.shape):
            raise ValueError(
                "Window/bounds is outside dataset extent, boundless reads are disabled"
            )

        c, _, _, f = window_bounds(win, self.transform)  # c ~ west, f ~ north
        a, b, _, d, e, _, _, _, _ = tuple(self.transform)
        new_affine = Affine(a, b, c, d, e, f)

        return new_affine

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.src is not None:
            # close the rasterio context manager
            self.src.close()


def boundless_array(arr, window, nodata, masked=False):
    dim3 = False
    if len(arr.shape) == 3:
        dim3 = True
    elif len(arr.shape) != 2:
        raise ValueError("Must be a 2D or 3D array")

    # unpack for readability
    (wr_start, wr_stop), (wc_start, wc_stop) = window

    # Calculate overlap
    olr_start = max(min(window[0][0], arr.shape[-2:][0]), 0)
    olr_stop = max(min(window[0][1], arr.shape[-2:][0]), 0)
    olc_start = max(min(window[1][0], arr.shape[-2:][1]), 0)
    olc_stop = max(min(window[1][1], arr.shape[-2:][1]), 0)

    # Calc dimensions
    overlap_shape = (olr_stop - olr_start, olc_stop - olc_start)
    if dim3:
        window_shape = (arr.shape[0], wr_stop - wr_start, wc_stop - wc_start)
    else:
        window_shape = (wr_stop - wr_start, wc_stop - wc_start)

    # create an array of nodata values
    out = np.empty(shape=window_shape, dtype=arr.dtype)
    out[:] = nodata

    # Fill with data where overlapping
    nr_start = olr_start - wr_start
    nr_stop = nr_start + overlap_shape[0]
    nc_start = olc_start - wc_start
    nc_stop = nc_start + overlap_shape[1]
    if dim3:
        out[:, nr_start:nr_stop, nc_start:nc_stop] = arr[
            :, olr_start:olr_stop, olc_start:olc_stop
        ]
    else:
        out[nr_start:nr_stop, nc_start:nc_stop] = arr[
            olr_start:olr_stop, olc_start:olc_stop
        ]

    if masked:
        out = np.ma.MaskedArray(out, mask=(out == nodata))

    return out


def rowcol(x, y, transform, rounding=np.floor):
    """Get row/col for a x/y"""
    r = int(rounding((y - transform.f) / transform.e))
    c = int(rounding((x - transform.c) / transform.a))
    return r, c


def bounds_window(bounds, transform):
    w, s, e, n = bounds
    row_start, col_start = rowcol(w, n, transform)
    row_stop, col_stop = rowcol(e, s, transform, rounding=np.ceil)
    return (row_start, row_stop), (col_start, col_stop)


def window_bounds(window, transform):
    (row_start, row_stop), (col_start, col_stop) = window
    w, s = transform * (col_start, row_stop)
    e, n = transform * (col_stop, row_start)
    return w, s, e, n


def beyond_extent(window, shape):
    """Checks if window references pixels beyond the raster extent"""
    (wr_start, wr_stop), (wc_start, wc_stop) = window
    return wr_start < 0 or wc_start < 0 or wr_stop > shape[0] or wc_stop > shape[1]
