import argparse
import functools
import httplib2
import os
import pprint
import sys

from apiclient import discovery
from dateutil.relativedelta import relativedelta
from dateutil.tz import tzutc, tzlocal
from datetime import datetime
from oauth2client import client, file, tools
from pyicl import Interval, IntervalMap, IntervalSet, Set
from pyrfc3339 import generate, parse
from utility import log_diag, log_err


# for appending to usernames
EMAIL_POSTFIX = '@onid.oregonstate.edu'


# Parser for command-line arguments.
parser = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 parents=[tools.argparser])


# CLIENT_SECRETS is name of a file containing the OAuth 2.0 information for
# this application, including client_id and client_secret. You can see the
# Client ID and Client secret on the APIs page in the Cloud Console:
# <https://cloud.google.com/console#/project/377626221408/apiui>
CLIENT_SECRETS = os.path.join(os.path.dirname(__file__),
                              'data/client_secrets.json')


# Set up a Flow object to be used for authentication.
# Add one or more of the following scopes. PLEASE ONLY ADD THE SCOPES YOU
# NEED. For more information on using scopes please see
# <https://developers.google.com/+/best-practices>.
FLOW = client.flow_from_clientsecrets(CLIENT_SECRETS,
    scope=[
        # Calendar API authorization
        #'https://www.googleapis.com/auth/calendar',
        'https://www.googleapis.com/auth/calendar.readonly',
        # Directory API authorization
        'https://www.googleapis.com/auth/admin.directory.device.chromeos',
        'https://www.googleapis.com/auth/admin.directory.device.chromeos.readonly',
        'https://www.googleapis.com/auth/admin.directory.device.mobile',
        'https://www.googleapis.com/auth/admin.directory.device.mobile.action',
        'https://www.googleapis.com/auth/admin.directory.device.mobile.readonly',
        'https://www.googleapis.com/auth/admin.directory.group',
        'https://www.googleapis.com/auth/admin.directory.group.member',
        'https://www.googleapis.com/auth/admin.directory.group.member.readonly',
        'https://www.googleapis.com/auth/admin.directory.group.readonly',
        'https://www.googleapis.com/auth/admin.directory.notifications',
        'https://www.googleapis.com/auth/admin.directory.orgunit',
        'https://www.googleapis.com/auth/admin.directory.orgunit.readonly',
        'https://www.googleapis.com/auth/admin.directory.user',
        'https://www.googleapis.com/auth/admin.directory.user.alias',
        'https://www.googleapis.com/auth/admin.directory.user.alias.readonly',
        'https://www.googleapis.com/auth/admin.directory.user.readonly',
        'https://www.googleapis.com/auth/admin.directory.user.security',
    ],
    message=tools.message_if_missing(CLIENT_SECRETS))


# Wraps a try-except block around attempts to construct a Google API query
def run_query(func):
    def try_run_query(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except client.AccessTokenRefreshError:
            print ("The credentials have been revoked or expired, please re-run"
                "the application to re-authorize")
    return try_run_query


# This decorator allows the deletion and selective setting of object
# attributes.  Its purpose is to prepare a GAPI object prior to executing
# certain methods, such as removing old calendar and query data defined on the
# instance prior to constructing and executing a new query.
def activate(attrs=[], **o_kwargs):
    def _activate(func):
        @functools.wraps(func)
        def _do_activate(obj, *i_args, **i_kwargs):
                if obj.active:
                    for attr in attrs:
                        attr = None
                for attr, val in o_kwargs.iteritems():
                    setattr(obj, attr, val)
                obj.active = True
                return func(obj, *i_args, **i_kwargs)
        return _do_activate
    return _activate


def nearest_hour(dt):
    hour = dt.hour
    if dt.minute > 30:
        hour += 1
    return dt.replace(hour=hour, minute=0, second=0, microsecond=0)


class GAPI(object):
    def __init__(self, api_name, api_version, auth_host_name=None,
                 noauth_local_webserver=None, auth_host_port=None,
                 logging_level=None,
                 storage_path=os.path.join(os.path.dirname(__file__),
                                           'data/sample.dat')):
        """
        For the calendar API, api_name is 'calendar' and api_version is 'v3'.
        For the directory API, api_name is 'admin' and api_version is 'directory_v1'
        """
        # Parse the command-line flags.
        # What to do about this?  It's unlikely that this module is going to be
        # invoked as a script.  Can we build the parser object without passing
        # sys.argv?  Maybe just an empty list?  Or does tools.run_flow accept other
        # structures for the 'flags' argument?
        flags = parser.parse_args(sys.argv[1:])

        if auth_host_name is not None:
            pass

        # Check that user has a graphical display available. If not, set the
        # flag that causes a link to the auth page to be displayed on the
        # command line
        if not os.environ.get('DISPLAY'):
            flags.noauth_local_webserver = True

        # If the credentials don't exist or are invalid run through the native
        # client flow. The Storage object will ensure that if successful the good
        # credentials will get written back to the file.
        storage = file.Storage(storage_path)
        credentials = storage.get()
        if credentials is None or credentials.invalid:
            credentials = tools.run_flow(FLOW, storage, flags)
        self.credentials = credentials

        # Create an httplib2.Http object to handle our HTTP requests and authorize
        # it with our good Credentials.
        http = httplib2.Http()
        self.http = credentials.authorize(http)

        # Construct the service object for the interacting with the Calendar API.
        self.service = discovery.build(api_name, api_version, http=http)


class CalendarAPI(GAPI):
    def __init__(self, tz=tzlocal(), **kwargs):
        super(CalendarAPI, self).__init__('calendar', 'v3', **kwargs)
        self.tz = tz
        self.active = False

        self.api = None
        self.calendars = None
        self.freebusy = None
        self.request = None
        self.onids = None

    # TODO: work on exception handling
    def build_freebusy_query(self, ids, timeMin, timeMax, timeZone=None,
                            groupExpansionMax=None, calendarExpansionMax=None):
        """
        (Adapted in part from Google's Freebusy docs at
        https://developers.google.com/google-apps/calendar/v3/reference/freebusy/query)

        Pre :
            - timeMin and timeMax are datetime strings of the form '%Y-%m-%dT%X',
            which is the format required by freebusy queries.
            - onids is a list of ONID usernames.
            - timeZone is a string representing a time zone.  Defaults to UTC.
            - groupExpansionMax is the maximal number of calendar identifiers to be
            provided for a single group.  Optional.  An error will be returned
            for a group with more members than this value.
            - calendarExpansionMax is the maximal number of calendars for which FreeBusy information is to be
            provided. Optional.
        Post:
            - Success: returns a freebusy query object
            - Failure: raises an exception if timeMin and timeMax are not specified

        """
        query = {}
        try:
            query['timeMin'] = timeMin
            query['timeMax'] = timeMax
            query['items'] = ids

            if timeZone is not None:
                query['timeZone'] = timeZone
            if groupExpansionMax is not None:
                query['groupExpansionMax'] = groupExpansionMax
            if calendarExpansionMax is not None:
                query['calendarExpansionMax'] = calendarExpansionMax

            return query

        except:
            print("Must specify timeMin and timeMax")

    # Decorates function with try-except block
    @run_query
    # Clears 'calendars' and 'freebusy' attribute, and sets 'active' to True
    @activate(['calendars', 'freebusy'])
    def run_freebusy_query(self, users, start_time=None, end_time=None,
                           suffix=EMAIL_POSTFIX, **kwargs):
        """ Returns the result of executing a freebusy query
        Pre:
            -   'users' is a collection of ONID usernames
            -   'start_time' and 'end_time' are datetime objects
            -   'suffix' is a string that is appended to the end of each
                username
        Post:
            -   Returns the result of executing a freebusy query
        """
        self.onids = [user + suffix for user in users]
        ids = [{'id': onid} for onid in self.onids]

        start_time, end_time = self._format_start_end(start_time, end_time)

        # Create strings from datetime objects so that we can pass them to the
        # Google freebusy API
        start_time_str = generate(start_time)
        end_time_str = generate(end_time)

        # Create the freebusy API and store in the object
        self.api = self.service.freebusy()

        # Create the freebusy query body
        query = self.build_freebusy_query(ids, start_time_str, end_time_str,
                                          **kwargs)

        # Create the request and store the result in the object
        self.request = self.api.query(body=query)

        # Return the executed request
        return self.request.execute()

    def query_calendars_free(self, users, start_time=None, end_time=None):
        """ Returns a calendar of the form returned by a freebusy query,
            but with 'free' as well as 'busy' times
        Pre:
            -   'users' is a collection of ONID usernames
            -   'start_time' and 'end_time' are datetime objects
        Post:
            -   Returns a calendar containing free and busy times
        """
        start_time, end_time = self._format_start_end(start_time, end_time)

        # Call the freebusy querying function and store the result in the
        # object
        self.freebusy = self.run_freebusy_query(users, start_time, end_time)

        # Pull out the calendars from the freebusy response
        calendars = self._extract_calendars()

        # Convert busy intervals from strings to datetime objects
        new_calendars = self.convert_calendars(calendars,
                                               self.get_ranges_datetime_obj,
                                               ['busy'])

        # Add list of free times to calendars.  Store the calendars in the
        # object, then return them.
        self.calendars = self._calendars_free(start_time, end_time, new_calendars)
        return self.calendars

    def get_calendars(self, tz=None):
        """
        Pre:
            -   'tz' is a 'tzinfo' object
        Post:
            -   Returns the object's 'calendars' attribute, after optionally
                converting the timezones of its 'free' and 'busy' times
        """
        if self.calendars is not None:
            calendars = self.calendars

            if tz is not None:
                calendars = self.convert_calendars(calendars,
                                                   self._convert_tz(tz))
            return calendars

        else:
            raise AttributeError("'CalendarAPI' object does not currently "
                                 "contain attribute 'calendars', please run "
                                 "query")

    def get_ranges_overlaps(self, users=None, start_time=None, end_time=None,
                            calendars=None, tz=None, status='free',
                            whole=False):
        """ Returns a list of dictionaries containing permutations of free
            times and users
        Pre:
            -   'calendars' is a list of calendars of the same structure as
                those extracted from the dictionary returned by
                apiclient.discovery.build().freebusy.query().execute()
            -   'tz' is a tzinfo object
            -   'status' is either 'free' or 'busy'
            -   'whole' is a Boolean value
        Post:
            -   Returns a list of dictionaries of the form:
                [{'start': <datetime>, 'end': <datetime>,
                'onids': ['user1@onid.oregonstate.edu',
                            'user2@onid.oregonstate.edu', ...]}]
            -   If 'whole' is 'True', returns only those times for which ALL
                people of interest are free.
        """
        if calendars is None:
            calendars = self.calendars
            if calendars is None:
                if users is None:
                    raise Exception("Cannot query free/busy info for no users")
                    return
                calendars = self.query_calendars_free(users, start_time, end_time)

        if tz is not None:
                calendars = self.convert_calendars(calendars,
                                                   self._convert_tz(tz))

        ranges_map = self._ranges_overlaps(calendars, status)

        ranges_overlaps = [
            {'onids': [u for u in segment.value],
             'start': segment.interval.upper,
             'end': segment.interval.lower}
            for segment in ranges_map
        ]

        if whole:
            onid_set = set(self.onids)
            return [r for r in ranges_overlaps if set(r.get('onids')) ==
                    onid_set]

        return ranges_overlaps

    def to_tz(self, tz, dt):
        """
        Converts a datetime object to a different timezone
        """
        if tz is None:
            tz = self.tz
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz=tz)

    def _convert_tz(self, tz=tzutc()):
        return functools.partial(self._convert_ranges_dict,
                                 functools.partial(self.to_tz, tz))

    def _datetimes_to_utc(self, *args):
        ret = []
        for dt in args:
            ret.append(self.to_tz(tzutc(), dt))
        if len(ret) < 2:
            return ret[0]
        else:
            return ret

    def _format_start_end(self, start_time, end_time):
        # Provide sane default for start and end times
        if start_time is None:
            start_time = nearest_hour(datetime.now(tz=tzutc()))

        if end_time is None:
            end_time = start_time + relativedelta(hours=+1)

        return self._datetimes_to_utc(start_time, end_time)

    def _convert_ranges_dict(self, convert_func, ranges_list):
        """
        Pre:
            -   convert_func is a function that takes datetime strings of the sort
                returned by a freebusy query and returns some value (usually a
                transformation of the string into some other representation of
                datetime).
            -   ranges_list is a list of dictionaries of the sort that is returned as the
                'busy' member of a freebusy query dictionary.  At minimum, it must
                have values for 'start' and 'end'.
        Post:
            -   Returns a list of dictionaries, each of which has keys 'start' and
                'end'.
        """
        ranges_list = [(convert_func(t.get('start')), convert_func(t.get('end')))
                    for t in ranges_list]
        return map(lambda e: {'start': e[0], 'end': e[1]}, ranges_list)

    def get_ranges_datetime_obj(self, *args, **kwargs):
        """
        Wrapper for _convert_ranges_dict() for use in map(), filter(), or
        another such function that requires a function of one argument
        """
        return functools.partial(self._convert_ranges_dict, parse)(*args, **kwargs)

    def _extract_calendars(self, freebusy=None):
        if freebusy is None:
            freebusy = self.freebusy
        return freebusy.get('calendars')

    def _disc_interval_set(self, pairs):
        """
        Pre:
            - pairs is a list of two-tuples.
        Post:
            Returns an IntervalSet containing each of the value sets in 'pairs'.
        """
        _disc_interval = IntervalSet()
        for l, r in pairs:
            _disc_interval.add(Interval(l, r))
        return _disc_interval

    def _interval_set_to_list(self, interval_set):
        """
        Pre:
            -   interval_set is an IntervalSet.
        Post:
            -   returns a list of two-tuples, one two-tuple per each interval in
                the interval_set.
        """
        return map(lambda elem: (elem.lower, elem.upper), interval_set)

    def convert_calendars(self, calendars, convert_func, statuses=['free', 'busy']):
        """
            Pre:
            -   'calendars' is a list of calendars of the same structure as those
                extracted from the dictionary returned by
                apiclient.discovery.build().freebusy.query().execute()
            -   'statuses' is a list containing either 'free' or 'busy' or both
            -   'convert_func' is a function that takes a list of dictionaries of
                the form {'start': <time_representation>, 'busy':
                    <time_representation>} and
        """
        if not statuses:
            raise ValueError(
                "'statuses' list must contain either 'free', 'busy', or both")

        # Copy the calendar so that we don't accidentally modify something we
        # shouldn't
        calendars_local = calendars.copy()

        for status in statuses:
            if status not in ['free', 'busy']:
                raise KeyError("'status' argument must be either 'free' or 'busy'")

            for account, range_dict in calendars_local.iteritems():
                # Access the list of free/busy times
                times = range_dict.get(status)

                # If the set of busy times is empty, then the account holder is
                # free for the entire interval
                if times == []:
                    continue

                # Build a list of busy ranges expressed as a dictionary with
                # 'start' and 'end' keys and values expressed as seconds since
                # the epoch
                try:
                    converted_ranges = convert_func(times)
                except:
                    log_err("Invalid conversion function")

                # Replace original range_dictionary with new range_dictionary
                range_dict[status] = converted_ranges

        return calendars_local

    def _ranges_overlaps(self, calendars, status):
        """
        Pre:
            -   calendars: a dictionary of the type returned by a freebusy query,
                except that they contain both 'free' AND 'busy' times, and the
                start and end times are represented by Python datetime objects.
            -   status: either the string 'free' or the string 'busy'
        Post:
            -   Returns a PyICL IntervalMap containing all sets of datetimes during
                which all calendars overlap for a given status.  In other words,
                for all users in the calendar dictionary, returns the times
                during which all are either free or busy, depending on the value
                of the 'status' argument.
        """
        if status not in ['free', 'busy']:
            raise KeyError("'status' argument must be either 'free' or 'busy'")

        calendars_local = calendars.copy()

        overlaps = IntervalMap()

        for account, ranges_dict in calendars_local.iteritems():
            # Access the list of free times
            free_ranges = ranges_dict.get(status)

            # Create a PyICL set containing the name of the account.
            #
            # N.B.: the second set of parentheses around 'account' and the
            # comma afterward are mandatory!
            account_set = Set((account,))

            # Create a map segment for each free interval the account holder has,
            # and add it to the map
            for free_range in free_ranges:
                segment = overlaps.Segment(Interval(free_range.get('start'),
                                                    free_range.get('end')),
                                        account_set)
                overlaps.add(segment)

        # 'overlaps' now contains a mapping of free times to sets of accounts.
        # 'overlaps', when iterated, will yield each segment.  These segments
        # have the members 'interval' (which holds the time interval) and value
        # (which holds the names of the accounts free at that time).  The start
        # time for each interval can be accessed by <segment name>.interval.lower,
        # and the end time by <segment name>.interval.upper.
        return overlaps

    def _calendars_free(self, start_time, end_time, calendars):
        """
        Pre:
            -   calendars: a dictionary of the type returned by a freebusy query
            -   time_min and time_max: datetime objects
        Post:
            -   Returns a dictionary of the same form as 'calendars', but containing
                a list of free times (key 'free') in addition to busy times
        """
        calendars_local = calendars.copy()

        # Create an IntervalSet with these times as endpoints
        whole_range = IntervalSet(Interval(start_time, end_time))

        # For each account in the calendar, transform the list of busy times
        # into a list of free times
        for account, ranges_dict in calendars_local.iteritems():
            # Access the list of busy times
            busy_ranges = ranges_dict.get('busy')

            # If the set of busy times is empty, then the account holder is
            # free for the entire interval
            if busy_ranges == []:
                busy_ranges_intervals = IntervalSet()
            else:
                # Build a list of busy ranges expressed as a dictionary with
                # 'start' and 'end' keys
                busy_ranges_tuples = map(lambda e: (e.get('start'),
                                                    e.get('end')), busy_ranges)

                # Turn this list into an IntervalSet
                busy_ranges_intervals = self._disc_interval_set(busy_ranges_tuples)

            # Take the difference of the whole range with the set of busy times
            # - this is an IntervalSet containing the account holder's free
            # intervals with units in seconds since the Epoch
            free_ranges_intervals = whole_range - busy_ranges_intervals
            free_ranges = self._interval_set_to_list(free_ranges_intervals)

            # Map list of tuples to dictionary in the form of the 'busy'
            # dictionary 'busy_ranges_dict'
            free_ranges_dict = map(lambda e: {'start': e[0], 'end': e[1]},
                                   free_ranges)

            # Store the dictionary of free times in the same dictionary as the
            # dictionary of busy times
            ranges_dict['free'] = free_ranges_dict

        # calendar_local now contains lists of both free and busy ranges for
        # each account holder
        return calendars_local


class DirectoryAPI(GAPI):
    def __init__(self, **kwargs):
        super(DirectoryAPI, self).__init__('admin', 'directory_v1', **kwargs)
