# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
CADC
====


Module to query the Canadian Astronomy Data Centre (CADC).
"""

import logging
import warnings
import requests
from numpy import ma
from urllib.error import HTTPError
from urllib.parse import urlencode
from pyvo.dal.adhoc import DatalinkResults
from ..utils.class_or_instance import class_or_instance
from ..utils import async_to_sync, commons
from ..query import BaseQuery
from bs4 import BeautifulSoup
from six import BytesIO
from astropy.io.votable import parse_single_table
from astroquery.utils.decorators import deprecated
from . import conf
try:
    import pyvo
except ImportError as e:
    msg = 'Please install pyvo. astropy.cadc does not work without it.'
    raise ImportError(msg)


__all__ = ['Cadc', 'CadcClass']

CADC_COOKIE_PREFIX = 'CADC_SSO'

logger = logging.getLogger(__name__)

# TODO figure out what do to if anything about them. Some might require
# fixes on the CADC servers
warnings.filterwarnings('ignore', module='astropy.io.votable')


@async_to_sync
class CadcClass(BaseQuery):
    """
    Class for accessing CADC data. Typical usage:

    result = Cadc.query_region('08h45m07.5s +54d18m00s', collection='CFHT')

    ... do something with result (optional) such as filter as in example below

    urls = Cadc.get_data_urls(result[result['target_name']=='Nr3491_1'])

    ... access data

    Other ways to query the CADC data storage:

    - target name:
        Cadc.query_region(SkyCoord.from_name('M31'))
    - target name in the metadata:
        Cadc.query_name('M31-A-6')  # queries as a like '%lower(name)%'
    - TAP query on the CADC metadata (CAOM2 format -
        http://www.opencadc.org/caom2/)
        Cadc.get_tables()  # list the tables
        Cadc.get_table(table_name)  # list table schema
        Cadc.query


    """

    CADC_REGISTRY_URL = conf.CADC_REGISTRY_URL
    CADCTAP_SERVICE_URI = conf.CADCTAP_SERVICE_URI
    CADCDATALINK_SERVICE_URI = conf.CADCDATLINK_SERVICE_URI
    CADCLOGIN_SERVICE_URI = conf.CADCLOGIN_SERVICE_URI
    TIMEOUT = conf.TIMEOUT

    def __init__(self, url=None, tap_handler=None, verbose=None):
        """
        Initialize Cadc object

        Parameters
        ----------
        url : str, optional, default 'None;
            a url to use instead of the default
        tap_plus_handler : deprecated
        verbose : deprecated

        Returns
        -------
        Cadc object
        """
        if tap_handler:
            warnings.warn('tap_handler deprecated since version 0.4.0')
        if verbose is not None:
            warnings.warn('verbose deprecated since version 0.4.0')

        super(CadcClass, self).__init__()
        if url is not None and tap_handler is not None:
            raise AttributeError('Can not input both url and tap handler')
        self.baseurl = url

    @property
    def cadctap(self):
        if not hasattr(self, '_cadctap'):
            if self.baseurl is None:
                self.baseurl = get_access_url(self.CADCTAP_SERVICE_URI)
                # remove capabilities endpoint to get to the service url
                self.baseurl = self.baseurl.rstrip('capabilities')
                self._cadctap = pyvo.dal.TAPService(self.baseurl)
            else:
                self._cadctap = pyvo.dal.TAPService(self.baseurl)
        return self._cadctap

    @property
    def data_link_url(self):
        if not hasattr(self, '_data_link_url'):
            self._data_link_url = get_access_url(
                self.CADCDATALINK_SERVICE_URI,
                "ivo://ivoa.net/std/DataLink#links-1.0")
        return self._data_link_url

    def login(self, user=None, password=None, certificate_file=None):
        """
        Login, set varibles to use for logging in

        Parameters
        ----------
        user : str, required if certificate is None
            username to login with
        password : str, required if user is set
            password to login with
        certificate : str, required if user is None
            path to certificate to use with logging in

        Notes
        -----
        This will soon be deprecated as it does not make sense to login with
        certificates.
        """
        if certificate_file:
            # In order to force an HTTPS hand shake on the session is to
            # recreate it.
            pyvo.dal.tap.s = requests.Session()
            pyvo.dal.tap.s.cert = certificate_file
        elif not user or not password:
            raise AttributeError('login credentials missing (user/password '
                                 'or certificate)')
        else:
            login_url = get_access_url(self.CADCLOGIN_SERVICE_URI,
                                       'ivo://ivoa.net/std/UMS#login-0.1')
            if login_url is None:
                raise RuntimeError("No login URL")
            # need to login and get a cookie
            args = {
                "username": str(user),
                "password": str(password)}
            header = {
                "Content-type": "application/x-www-form-urlencoded",
                "Accept": "text/plain"
            }
            response = requests.post(login_url, data=args, headers=header)
            try:
                response.raise_for_status()
            except Exception as e:
                logger.error('Logging error: {}'.format(e))
                raise e
            # extract cookie
            cookie = '"{}"'.format(response.text)
            if cookie is not None:
                pyvo.dal.tap.s.cookies.set(CADC_COOKIE_PREFIX, cookie)

    def logout(self, verbose=None):
        """
        Logout

        Parameters
        ----------
        verbose : deprecated

        Notes
        -----
        This method will soon be deprecated as it doesn't make sense to
        login and logout with certificates.
        """
        if verbose is not None:
            warnings.warn('verbose deprecated since 0.4.0')

        # the only way to ensure complete logout is to start with a new
        # session. This is mainly because of certificates. Adding cert
        # argument to a session already in use does not force it to
        # re-do the HTTPS hand shake
        pyvo.dal.tap.s = requests.Session()

    @class_or_instance
    def query_region_async(self, coordinates, radius=0.016666666666667,
                           collection=None,
                           get_query_payload=False):
        """
        Queries the CADC for a region around the specified coordinates.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates`.
            coordinates around which to query
        radius : str or `astropy.units.Quantity`.
            the radius of the cone search
        collection: Name of the CADC collection to query, optional
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters.

        Returns
        -------
        response : `requests.Response`
            The HTTP response returned from the service.
            All async methods should return the raw HTTP response.
        """

        request_payload = self._args_to_payload(coordinates=coordinates,
                                                radius=radius,
                                                collection=collection)
        # primarily for debug purposes, but also useful if you want to send
        # someone a URL linking directly to the data
        if get_query_payload:
            return request_payload
        response = self.exec_sync(request_payload['query'])
        return response

    @class_or_instance
    def query_name_async(self, name):
        """
        Query CADC metadata for a name and return the corresponding metadata in
         the CAOM2 format (http://www.opencadc.org/caom2/).

        Parameters
        ----------
        name: str
                name of object to query for

        Returns
        -------
        response : `~astropy.table.Table`
            Results of the query in a tabular format.

        """
        response = self.exec_sync(
            "select * from caom2.Observation o join caom2.Plane p "
            "on o.obsID=p.obsID where lower(target_name) like '%{}%'".
            format(name.lower()))
        return response

    @class_or_instance
    def get_collections(self):
        """
        Query CADC for all the hosted collections

        Returns
        -------
        A dictionary of collections hosted at the CADC where the key is the
        collection and value represents details of that collection.
        """
        response = self.exec_sync(
            'select distinct collection, energy_emBand from caom2.EnumField')
        collections = {}
        for row in response:
            if row['collection'] not in collections:
                collection = {
                    'Description': 'The {} collection at the CADC'.
                    format(row['collection']), 'Bands': []}
                if row['energy_emBand'] is not ma.masked:
                    collection['Bands'].append(row['energy_emBand'])
                collections[row['collection']] = collection
            elif row['energy_emBand'] is not ma.masked:
                collections[row['collection']]['Bands'].\
                    append(row['energy_emBand'])
        return collections

    @class_or_instance
    def get_images(self, coordinates, radius=0.016666666666667,
                   collection=None,
                   get_query_payload=False,
                   show_progress=True):
        """
        A coordinate-based query function that returns a list of
        fits files with cutouts around the passed in coordinates.

        Parameters
        ----------
        coordinates : str or `astropy.coordinates`.
            Coordinates around which to query
        radius : str or `astropy.units.Quantity`.
            The radius of the cone search AND cutout area
        collection: str, optional
            Name of the CADC collection to query, optional
        get_query_payload : bool, optional
            Just return the dict of HTTP request parameters.
        show_progress: bool, optional
            Whether to display a progress bar if the file is downloaded
            from a remote server.  Default is `True`.

        Returns
        -------
        list : A list of `~astropy.io.fits.HDUList` objects
        """

        request_payload = self._args_to_payload(coordinates=coordinates,
                                                radius=radius,
                                                collection=collection)
        request_payload['query'] = request_payload['query'] + " AND (dataProductType = 'image')"

        if get_query_payload:
            return request_payload

        query = request_payload['query'] + " AND (dataProductType = 'image')"
        response = self.run_query(query, operation='sync')
        query_result = response.get_results()

        if query_result and len(query_result) == 2000:
            logger.debug("Synchronous query results capped at 2000 results - results may be truncated")

        images_urls = self.get_image_list(query_result, coordinates, radius)
        images = []

        try:
            readable_objects = [commons.FileContainer(url, encoding='binary',
                                                      show_progress=show_progress) for url in images_urls]
            for obj in readable_objects:
                images.append(obj.get_fits())
        except HTTPError as err:
            logger.debug(
                "{} - Problem retrieving the file: {}".format(str(err), str(err.url)))
            pass

        return images

    @class_or_instance
    def get_image_list(self, query_result, coordinates, radius):
        """
        Function to map the results of a CADC query into URLs to
        corresponding data and cutouts that can be later downloaded.

        The function uses the IVOA DataLink Service
        (http://www.ivoa.net/documents/DataLink/) implemented at the CADC.
        It works directly with the results produced by Cadc.query_region and
        Cadc.query_name but in principle it can work with other query
        results produced with the Cadc query as long as the results
        contain the 'caomPublisherID' column. This column is part of the
        caom2.Plane table.

        Parameters
        ----------
        query_result : result returned by Cadc.query_region() or
                    Cadc.query_name(). In general, the result of any
                    CADC TAP query that contains the 'caomPublisherID' column
                    can be use here.
        coordinates : str or `astropy.coordinates`.
            Coordinates around which to query
        radius : str or `astropy.units.Quantity`.
            The radius of the cone search AND cutout area

        Returns
        -------
        A list of URLs to data.
        """

        def chunks(obj_list, chunk_len):
            """
            A generator that breaks list obj_list into sublists of length chunk_len
            :param obj_list: The list to be chunked
            :param chunk_len: The length of each chunked sublist
            :return: An iterator that goes through each sublist
            """
            for idx in range(0, len(obj_list), chunk_len):
                yield obj_list[idx:idx + chunk_len]

        if not query_result:
            raise AttributeError('Missing query_result argument')

        # Send datalink requests in batches of 20 publisher ids
        n_pids = 20
        parsed_coordinates = commons.parse_coordinates(coordinates).fk5
        ra = parsed_coordinates.ra.degree
        dec = parsed_coordinates.dec.degree
        cutout_params = {'POS': 'CIRCLE {} {} {}'.format(ra, dec, radius)}

        try:
            publisher_ids = query_result['caomPublisherID']
        except KeyError:
            raise AttributeError(
                'caomPublisherID column missing from query_result argument')
        result = []

        # Iterate through list of sublists to send datalink requests in batches
        for pid_sublist in chunks(publisher_ids, n_pids):
            datalink = DatalinkResults.from_result_url(
                '{}?{}'.format(self.data_link_url, urlencode({'ID': pid_sublist}, True)))
            for service_def in datalink.bysemantics('#cutout'):
                access_url = service_def.access_url.decode('ascii')
                if '/sync' in access_url:
                    service_params = service_def.input_params
                    input_params = {param.name: param.value for param in service_params if
                                    param.name in ['ID', 'RUNID']}
                    input_params.update(cutout_params)
                    result.append('{}?{}'.format(access_url, urlencode(input_params)))

        return result

    @class_or_instance
    def get_data_urls(self, query_result, include_auxiliaries=False):
        """
        Function to map the results of a CADC query into URLs to
        corresponding data that can be later downloaded.

        The function uses the IVOA DataLink Service
        (http://www.ivoa.net/documents/DataLink/) implemented at the CADC.
        It works directly with the results produced by Cadc.query_region and
        Cadc.query_name but in principle it can work with other query
        results produced with the Cadc query as long as the results
        contain the 'caomPublisherID' column. This column is part of the
        caom2.Plane table.

        Parameters
        ----------
        query_result : result returned by Cadc.query_region() or
                    Cadc.query_name(). In general, the result of any
                    CADC TAP query that contains the 'caomPublisherID' column
                    can be use here.
        include_auxiliaries : boolean
                    True to return URLs to auxiliary files such as
                    previews, False otherwise

        Returns
        -------
        A list of URLs to data.
        """

        if not query_result:
            raise AttributeError('Missing metadata argument')

        try:
            publisher_ids = query_result['publisherID']
        except KeyError:
            raise AttributeError(
                'caomPublisherID column missing from query_result argument')
        result = []
        for pid in publisher_ids:
            response = self._request('GET', self.data_link_url,
                                     params={'ID': pid})
            response.raise_for_status()
            buffer = BytesIO(response.content)

            # at this point we don't need cutouts or other SODA services so
            # just get the urls from the response VOS table
            tb = parse_single_table(buffer)
            for row in tb.array:
                semantics = row['semantics'].decode('ascii')
                if semantics == '#this':
                    result.append(row['access_url'].decode('ascii'))
                elif row['access_url'] and include_auxiliaries:
                    result.append(row['access_url'].decode('ascii'))
        return result

    def get_tables(self, only_names=False, verbose=None):
        """
        Gets all public tables

        Parameters
        ----------
        only_names : bool, optional, default 'False'
            True to load table names only
        verbose : deprecated

        Returns
        -------
        A list of table objects
        """
        if verbose is not None:
            warnings.warn('verbose deprecated since 0.4.0')
        table_set = self.cadctap.tables
        if only_names:
            return list(table_set.keys())
        else:
            return list(table_set.values())

    def get_table(self, table, verbose=None):
        """
        Gets the specified table

        Parameters
        ----------
        table : str, mandatory
            full qualified table name (i.e. schema name + table name)
        verbose : deprecated

        Returns
        -------
        A table object
        """
        if verbose is not None:
            warnings.warn('verbose deprecated since 0.4.0')
        tables = self.get_tables()
        for t in tables:
            if table == t.name:
                return t

    def exec_sync(self, query, maxrec=None, uploads=None, output_file=None):
        """
        Run a query and return the results or save them in a output_file

        Parameters
        ----------
        query : str, mandatory
            SQL to execute
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads:
            Temporary tables to upload and run with the queries
        output_file: str or file handler:
            File to save the results to

        Returns
        -------
        Results of running the query in (for now) votable format

        Notes
        -----
        Support for other output formats (tsv, csv) to be added as soon
        as they are available in pyvo.
        """
        response = self.cadctap.search(query, language='ADQL',
                                       uploads=uploads)
        result = response.to_table()
        if output_file:
            if isinstance(output_file, str):
                with open(output_file, 'bw') as f:
                    f.write(result)
                    return
            else:
                output_file.write(result)
                return
        return result

    def create_async(self, query, maxrec=None, uploads=None):
        """
        Creates a TAP job to execute and returns it to the caller. The
        caller then can start the execution and monitor the job.
        Typical (no error handling) sequence of events:

        job = create_async(query)
        job = job.run().wait()
        job.raise_if_error()
        result = job.fetch_result()
        job.delete() # optional

        See ``pyvo.dal.tap`` for details about the `AsyncTAPJob`

        Parameters
        ----------
        query : str, mandatory
            SQL to execute
        maxrec : int
            the maximum records to return. defaults to the service default
        uploads:
            Temporary tables to upload and run with the queries
        output_file: str or file handler:
            File to save the results to

        Returns
        -------
        AsyncTAPJob
            the query instance

        Notes
        -----
        Support for other output formats (tsv, csv) to be added as soon
        as they are available in pyvo.
        """
        return self.cadctap.submit_job(query, language='ADQL',
                                       uploads=uploads)

    @deprecated('0.4.0', 'Use axec_sync or create_async instead')
    def run_query(self, query, operation, output_file=None,
                  output_format="votable", verbose=None,
                  background=False, upload_resource=None,
                  upload_table_name=None):
        """
        Runs a query

        Parameters
        ----------
        query : str, mandatory
            query to be executed
        operation : str, mandatory,
            'sync' or 'async' to run a synchronous or asynchronous job
        output_file : str, optional, default None
            file name where the results are saved if dumpToFile is True.
            If this parameter is not provided, the jobid is used instead
        output_format : str, optional, default 'votable'
            results format, 'csv', 'tsv' and 'votable'
        verbose : deprecated
        save_to_file : bool, optional, default 'False'
            if True, the results are saved in a file instead of using memory
        background : bool, optional, default 'False'
            when the job is executed in asynchronous mode,
            this flag specifies whether the execution will wait until results
            are available
        upload_resource: str, optional, default None
            resource to be uploaded to UPLOAD_SCHEMA
        upload_table_name: str, required if uploadResource is provided,
            default None
            resource temporary table name associated to the uploaded resource

        Returns
        -------
        A Job object
        """
        # if verbose is not None:
        #     warnings.warn('verbose deprecated since 0.4.0')
        # if output_file is not None:
        #     save_to_file = True
        # else:
        #     save_to_file = False
        # uploads = {} # TODO
        # if operation == 'sync':
        #     self.exec_sync(query)
        # elif operation == 'async':
        #     job = AsyncTAPJob.create(
        #         self.baseurl, query, 'ADQL', None, uploads)
        #     job = job.run().wait()
        #     job.raise_if_error()
        #     result = job.fetch_result()
        #     job.delete()
        #
        # if save_to_file:
        #     raise NotImplementedError("TODO")
        #     cjob.save_results(output_file)
        # else:
        #     job.get_results = job.results
        # return cjob

    def load_async_job(self, jobid, verbose=None):
        """
        Loads an asynchronous job

        Parameters
        ----------
        jobid : str, mandatory
            job identifier
        verbose : deprecated

        Returns
        -------
        A Job object
        """
        if verbose is not None:
            warnings.warn('verbose deprecated since 0.4.0')

        return pyvo.dal.AsyncTAPJob('{}/async/{}'.format(
            self.cadctap.baseurl, jobid))

    def list_async_jobs(self, verbose=None):
        """
        Returns all the asynchronous jobs

        Parameters
        ----------
        verbose : deprecated

        Returns
        -------
        A list of Job objects
        """
        if verbose is not None:
            warnings.warn('verbose deprecated since 0.4.0')

        raise NotImplementedError(
            'Broken since pyvo does not support this yet')

    def _parse_result(self, result, verbose=None):
        return result

    def _args_to_payload(self, *args, **kwargs):
        # convert arguments to a valid requests payload
        # and force the coordinates to FK5 (assuming FK5/ICRS are
        # interchangeable) since RA/Dec are used below
        coordinates = commons.parse_coordinates(kwargs['coordinates']).fk5
        radius = kwargs['radius']
        payload = {format: 'VOTable'}
        payload['query'] = \
            "SELECT * from caom2.Observation o join caom2.Plane p " \
            "ON o.obsID=p.obsID " \
            "WHERE INTERSECTS( " \
            "CIRCLE('ICRS', {}, {}, {}), position_bounds) = 1 AND " \
            "(quality_flag IS NULL OR quality_flag != 'junk')".\
            format(coordinates.ra.degree, coordinates.dec.degree, radius)
        if 'collection' in kwargs and kwargs['collection']:
            payload['query'] = "{} AND collection='{}'".\
                format(payload['query'], kwargs['collection'])
        return payload


def static_vars(**kwargs):
    def decorate(func):
        for k in kwargs:
            setattr(func, k, kwargs[k])
        return func
    return decorate


@static_vars(caps={})
def get_access_url(service, capability=None):
    """
    Returns the URL corresponding to a service by doing a lookup in the cadc
    registry. It returns the access URL corresponding to cookie authentication.
    :param service: the service the capability belongs to. It can be identified
    by a CADC uri ('ivo://cadc.nrc.ca/) which is looked up in the CADC registry
    or by the URL where the service capabilities is found.
    :param capability: uri representing the capability for which the access
    url is sought
    :return: the access url

    Note
    ------
    This function implements the functionality of a CADC registry as defined
    by the IVOA. It should be eventually moved to its own directory.

    Caching should be considered to reduce the number of remote calls to
    CADC registry
    """

    caps_url = ''
    if service.startswith('http'):
        if not capability:
            return service
        caps_url = service
    else:
        # get caps from the CADC registry
        if not get_access_url.caps:
            try:
                response = requests.get(conf.CADC_REGISTRY_URL)
                response.raise_for_status()
            except requests.exceptions.HTTPError as err:
                logger.debug(
                    "ERROR getting the CADC registry: {}".format(str(err)))
                raise err
            for line in response.text.splitlines():
                if len(line) > 0 and not line.startswith('#'):
                    service_id, capabilies_url = line.split('=')
                    get_access_url.caps[service_id.strip()] = \
                        capabilies_url.strip()
        # lookup the service
        service_uri = service
        if not service.startswith('ivo'):
            # assume short form of CADC service
            service_uri = 'ivo://cadc.nrc.ca/{}'.format(service)
        if service_uri not in get_access_url.caps:
            raise AttributeError(
                "Cannot find the capabilities of service {}".format(service))
        # look up in the CADC reg for the service capabilities
        caps_url = get_access_url.caps[service_uri]
        if not capability:
            return caps_url
    try:
        response2 = requests.get(caps_url)
        response2.raise_for_status()
    except Exception as e:
        logger.debug(
            "ERROR getting the service capabilities: {}".format(str(e)))
        raise e

    soup = BeautifulSoup(response2.text, features="html5lib")
    for cap in soup.find_all('capability'):
        if cap.get("standardid", None) == capability:
            if len(cap.find_all('interface')) == 1:
                return cap.find_all('interface')[0].accessurl.text
            for i in cap.find_all('interface'):
                if hasattr(i, 'securitymethod'):
                    sm = i.securitymethod
                    if not sm or sm.get("standardid", None) is None or\
                       sm['standardid'] == "ivo://ivoa.net/sso#cookie":
                        return i.accessurl.text
    raise RuntimeError("ERROR - capabilitiy {} not found or not working with "
                       "anonymous or cookie access".format(capability))


Cadc = CadcClass()
