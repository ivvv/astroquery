# Licensed under a 3-clause BSD style license - see LICENSE.rst

"""
MAST Tesscut
============

Cutout queries on TESS FFIs.

"""

from __future__ import print_function, division

import warnings
import time
import json
import zipfile
import os

from io import BytesIO

import numpy as np

import astropy.units as u
from astropy.coordinates import Angle

from astropy.table import Table
from astropy.io import fits

from ..query import BaseQuery
from ..utils import commons
from ..exceptions import NoResultsWarning, InvalidQueryError, RemoteServiceError

from . import conf
from .core import Mast

__all__ = ["TesscutClass", "Tesscut"]


def _parse_input_location(coordinates=None, objectname=None):
    """
    Convenience function to parse user input of coordinates and objectname.

    Parameters
    ----------
    coordinates : str or `astropy.coordinates` object, optional
        The target around which to search. It may be specified as a
        string or as the appropriate `astropy.coordinates` object.
        One and only one of coordinates and objectname must be supplied.
    objectname : str, optional
        The target around which to search, by name (objectname="M104")
        or TIC ID (objectname="TIC 141914082").
        One and only one of coordinates and objectname must be supplied.

    Returns
    -------
    response : `~astropy.coordinates.SkyCoord`
        The given coordinates, or object's location as an `~astropy.coordinates.SkyCoord` object.
    """

    # Checking for valid input
    if objectname and coordinates:
        raise InvalidQueryError("Only one of objectname and coordinates may be specified.")

    if not (objectname or coordinates):
        raise InvalidQueryError("One of objectname and coordinates must be specified.")

    if objectname:
        obj_coord = Mast.resolve_object(objectname)

    if coordinates:
        obj_coord = commons.parse_coordinates(coordinates)

    return obj_coord


class TesscutClass(BaseQuery):
    """
    MAST TESS FFI cutout query class.

    Class for accessing TESS full-frame image cutouts.
    """

    def __init__(self):

        super(TesscutClass, self).__init__()

        self._TESSCUT_URL = conf.server + "/tesscut/api/v0.1/"

    def get_sectors(self, coordinates=None, radius=0.2*u.deg, objectname=None):
        """
        Get a list of the TESS data sectors whose footprints intersect
        with the given search area.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates` object, optional
            The target around which to search. It may be specified as a
            string or as the appropriate `astropy.coordinates` object.
            One and only one of coordinates and objectname must be supplied.
        radius : str, float, or `~astropy.units.Quantity` object, optional
            Default 0.2 degrees.
            If supplied as a float degrees is the assumed unit.
            The string must be parsable by `~astropy.coordinates.Angle`. The
            appropriate `~astropy.units.Quantity` object from
            `astropy.units` may also be used.
        objectname : str, optional
            The target around which to search, by name (objectname="M104")
            or TIC ID (objectname="TIC 141914082").
            One and only one of coordinates and objectname must be supplied.

        Returns
        -------
        response : `~astropy.table.Table`
            Sector/camera/chip information for given coordinates/raduis.
        """

        # Get Skycoord object for coordinates/object
        coordinates = _parse_input_location(coordinates, objectname)

        # If radius is just a number we assume degrees
        if isinstance(radius, (int, float)):
            radius = radius * u.deg
        radius = Angle(radius)

        sector_request = "ra={}&dec={}&radius={}d".format(coordinates.ra.deg,
                                                          coordinates.dec.deg,
                                                          radius.deg)
        response = self._request("GET", self._TESSCUT_URL+"sector",
                                 params=sector_request)

        response.raise_for_status()  # Raise any errors

        sector_json = response.json()['results']
        sector_dict = {'sectorName': [],
                       'sector': [],
                       'camera': [],
                       'ccd': []}

        for entry in sector_json:
            sector_dict['sectorName'].append(entry['sectorName'])
            sector_dict['sector'].append(int(entry['sector']))
            sector_dict['camera'].append(int(entry['camera']))
            sector_dict['ccd'].append(int(entry['ccd']))

        if not len(sector_json):
            warnings.warn("Coordinates are not in any TESS sector.", NoResultsWarning)
        return Table(sector_dict)

    def download_cutouts(self, coordinates=None, size=5, sector=None, path=".", inflate=True, objectname=None):
        """
        Download cutout target pixel file(s) around the given coordinates with indicated size.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates` object, optional
            The target around which to search. It may be specified as a
            string or as the appropriate `astropy.coordinates` object.
            One and only one of coordinates and objectname must be supplied.
        size : int, array-like, `~astropy.units.Quantity`
            Optional, default 5 pixels.
            The size of the cutout array. If ``size`` is a scalar number or
            a scalar `~astropy.units.Quantity`, then a square cutout of ``size``
            will be created.  If ``size`` has two elements, they should be in
            ``(ny, nx)`` order.  Scalar numbers in ``size`` are assumed to be in
            units of pixels. `~astropy.units.Quantity` objects must be in pixel or
            angular units.
        sector : int
            Optional.
            The TESS sector to return the cutout from.  If not supplied, cutouts
            from all available sectors on which the coordinate appears will be returned.
        path : str
            Optional.
            The directory in which the cutouts will be saved.
            Defaults to current directory.
        inflate : bool
            Optional, default True.
            Cutout target pixel files are returned from the server in a zip file,
            by default they will be inflated and the zip will be removed.
            Set inflate to false to stop before the inflate step.
        objectname : str, optional
            The target around which to search, by name (objectname="M104")
            or TIC ID (objectname="TIC 141914082").
            One and only one of coordinates and objectname must be supplied.

        Returns
        -------
        response : `~astropy.table.Table`
        """

        # Get Skycoord object for coordinates/object
        coordinates = _parse_input_location(coordinates, objectname)

        # Making size into an array [ny, nx]
        if np.isscalar(size):
            size = np.repeat(size, 2)

        if isinstance(size, u.Quantity):
            size = np.atleast_1d(size)
            if len(size) == 1:
                size = np.repeat(size, 2)

        if len(size) > 2:
            warnings.warn("Too many dimensions in cutout size, only the first two will be used.",
                          InputWarning)

        # Getting x and y out of the size
        if np.isscalar(size[0]):
            x = size[1]
            y = size[0]
            units = "px"
        elif size[0].unit == u.pixel:
            x = size[1].value
            y = size[0].value
            units = "px"
        elif size[0].unit.physical_type == 'angle':
            x = size[1].to(u.deg).value
            y = size[0].to(u.deg).value
            units = "d"
        else:
            raise InvalidQueryError("Cutout size must be in pixels or angular quantity.")

        path = os.path.join(path, '')
        astrocut_request = "ra={}&dec={}&y={}&x={}&units={}".format(coordinates.ra.deg,
                                                                    coordinates.dec.deg,
                                                                    y, x, units)
        if sector:
            astrocut_request += "&sector={}".format(sector)

        astrocut_url = self._TESSCUT_URL + "astrocut?" + astrocut_request
        zipfile_path = "{}tesscut_{}.zip".format(path, time.strftime("%Y%m%d%H%M%S"))

        self._download_file(astrocut_url, zipfile_path)

        localpath_table = Table(names=["Local Path"], dtype=[str])

        # Checking if we got a zip file or a json no results message
        if not zipfile.is_zipfile(zipfile_path):
            with open(zipfile_path, 'r') as FLE:
                response = json.load(FLE)
            warnings.warn(response['msg'], NoResultsWarning)
            return localpath_table

        if not inflate:  # not unzipping
            localpath_table['Local Path'] = [zipfile_path]
            return localpath_table

        print("Inflating...")
        # unzipping the zipfile
        zip_ref = zipfile.ZipFile(zipfile_path, 'r')
        cutout_files = zip_ref.namelist()
        zip_ref.extractall(path, members=cutout_files)
        zip_ref.close()
        os.remove(zipfile_path)

        localpath_table['Local Path'] = [path+x for x in cutout_files]
        return localpath_table

    def get_cutouts(self, coordinates=None, size=5, sector=None, objectname=None):
        """
        Get cutout target pixel file(s) around the given coordinates with indicated size,
        and return them as a list of  `~astropy.io.fits.HDUList` objects.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates` object, optional
            The target around which to search. It may be specified as a
            string or as the appropriate `astropy.coordinates` object.
            One and only one of coordinates and objectname must be supplied.
        size : int, array-like, `~astropy.units.Quantity`
            Optional, default 5 pixels.
            The size of the cutout array. If ``size`` is a scalar number or
            a scalar `~astropy.units.Quantity`, then a square cutout of ``size``
            will be created.  If ``size`` has two elements, they should be in
            ``(ny, nx)`` order.  Scalar numbers in ``size`` are assumed to be in
            units of pixels. `~astropy.units.Quantity` objects must be in pixel or
            angular units.
        sector : int
            Optional.
            The TESS sector to return the cutout from.  If not supplied, cutouts
            from all available sectors on which the coordinate appears will be returned.
        objectname : str, optional
            The target around which to search, by name (objectname="M104")
            or TIC ID (objectname="TIC 141914082").
            One and only one of coordinates and objectname must be supplied.

        Returns
        -------
        response : A list of `~astropy.io.fits.HDUList` objects.
        """

        # Get Skycoord object for coordinates/object
        coordinates = _parse_input_location(coordinates, objectname)

        # Making size into an array [ny, nx]
        if np.isscalar(size):
            size = np.repeat(size, 2)

        if isinstance(size, u.Quantity):
            size = np.atleast_1d(size)
            if len(size) == 1:
                size = np.repeat(size, 2)

        if len(size) > 2:
            warnings.warn("Too many dimensions in cutout size, only the first two will be used.",
                          InputWarning)

        # Getting x and y out of the size
        if np.isscalar(size[0]):
            x = size[1]
            y = size[0]
            units = "px"
        elif size[0].unit == u.pixel:
            x = size[1].value
            y = size[0].value
            units = "px"
        elif size[0].unit.physical_type == 'angle':
            x = size[1].to(u.deg).value
            y = size[0].to(u.deg).value
            units = "d"
        else:
            raise InvalidQueryError("Cutout size must be in pixels or angular quantity.")

        astrocut_request = "ra={}&dec={}&y={}&x={}&units={}".format(coordinates.ra.deg,
                                                                    coordinates.dec.deg,
                                                                    y, x, units)
        if sector:
            astrocut_request += "&sector={}".format(sector)

        response = self._request("GET", self._TESSCUT_URL+"astrocut", params=astrocut_request)
        response.raise_for_status()  # Raise any errors

        try:
            ZIPFILE = zipfile.ZipFile(BytesIO(response.content), 'r')
        except zipfile.BadZipFile:
            message = response.json()
            warnings.warn(message['msg'], NoResultsWarning)
            return []

        # Open all the contained fits files:
        # Since we cannot seek on a compressed zip file,
        # we have to read the data, wrap it in another BytesIO object,
        # and then open that using fits.open
        cutout_hdus_list = []
        for name in ZIPFILE.namelist():
            CUTOUT = BytesIO(ZIPFILE.open(name).read())
            cutout_hdus_list.append(fits.open(CUTOUT))

            # preserve the original filename in the fits object
            cutout_hdus_list[-1].filename = name

        return cutout_hdus_list


Tesscut = TesscutClass()
