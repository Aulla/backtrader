#!/usr/bin/env python
# -*- coding: utf-8; py-indent-offset:4 -*-
###############################################################################
#
# Copyright (C) 2015, 2016 Daniel Rodriguez
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
###############################################################################
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import collections
import datetime
import inspect
import io
import os.path

import backtrader as bt
from backtrader import (date2num, num2date, time2num, TimeFrame, dataseries,
                        metabase)

from backtrader.utils.py3 import with_metaclass, zip, range
from .dataseries import SimpleFilterWrapper
from .resamplerfilter import Resampler, Replayer


class MetaAbstractDataBase(dataseries.OHLCDateTime.__class__):
    _indcol = dict()

    def __init__(cls, name, bases, dct):
        '''
        Class has already been created ... register subclasses
        '''
        # Initialize the class
        super(MetaAbstractDataBase, cls).__init__(name, bases, dct)

        if not cls.aliased and \
           name != 'DataBase' and not name.startswith('_'):
            cls._indcol[name] = cls

    def dopreinit(cls, _obj, *args, **kwargs):
        _obj, args, kwargs = \
            super(MetaAbstractDataBase, cls).dopreinit(_obj, *args, **kwargs)

        # Find the owner and store it
        _obj._feed = metabase.findowner(_obj, FeedBase)

        _obj.notifs = collections.deque()  # store notifications for cerebro

        return _obj, args, kwargs

    def dopostinit(cls, _obj, *args, **kwargs):
        _obj, args, kwargs = \
            super(MetaAbstractDataBase, cls).dopostinit(_obj, *args, **kwargs)

        _obj._name = _obj.p.name
        _obj._compression = _obj.p.compression
        _obj._timeframe = _obj.p.timeframe

        if isinstance(_obj.p.sessionstart, datetime.datetime):
            _obj.p.sessionstart = _obj.p.sessionstart.time()

        elif _obj.p.sessionstart is None:
            _obj.p.sessionstart = datetime.time.min

        if isinstance(_obj.p.sessionend, datetime.datetime):
            _obj.p.sessionend = _obj.p.sessionend.time()

        elif _obj.p.sessionend is None:
            _obj.p.sessionend = datetime.time.max

        if isinstance(_obj.p.fromdate, datetime.date):
            # push it to the end of the day, or else intraday
            # values before the end of the day would be gone
            if not hasattr(_obj.p.fromdate, 'hour'):
                _obj.p.fromdate = datetime.datetime.combine(
                    _obj.p.fromdate, _obj.p.sessionstart)

        if isinstance(_obj.p.todate, datetime.date):
            # push it to the end of the day, or else intraday
            # values before the end of the day would be gone
            if not hasattr(_obj.p.todate, 'hour'):
                _obj.p.todate = datetime.datetime.combine(
                    _obj.p.todate, _obj.p.sessionend)

        # hold datamaster points corresponding to own
        _obj.mlen = list()

        _obj._barstack = collections.deque()  # for filter operations

        _obj._filters = list()
        _obj._ffilters = list()
        for fp in _obj.p.filters:
            if inspect.isclass(fp):
                fp = fp(_obj)
                if hasattr(fp, 'last'):
                    _obj._ffilters.append((fp, [], {}))

            _obj._filters.append((fp, [], {}))

        return _obj, args, kwargs


class AbstractDataBase(with_metaclass(MetaAbstractDataBase,
                                      dataseries.OHLCDateTime)):

    params = (('dataname', None),
              ('name', ''),
              ('compression', 1),
              ('timeframe', TimeFrame.Days),
              ('fromdate', None),
              ('todate', None),
              ('sessionstart', None),
              ('sessionend', None),
              ('filters', []),
              ('tz', None),
              ('tzinput', None),
    )

    (CONNECTED, DISCONNECTED, CONNBROKEN, DELAYED,
     LIVE, NOTSUBSCRIBED, UNKNOWN) = range(7)

    _NOTIFNAMES = [
        'CONNECTED', 'DISCONNECTED', 'CONNBROKEN', 'DELAYED', 'LIVE',
        'NOTSUBSCRIBED', 'UNKNOWN']

    @classmethod
    def _getstatusname(cls, status):
        return cls._NOTIFNAMES[status]

    _feed = None
    _tmoffset = datetime.timedelta()

    # Set to non 0 if resampling/replaying
    resampling = 0
    replaying = 0

    def _start(self):
        self.start()

        # A live feed (for example) may have learnt something about the
        # timezones after the start and that's why the date/time related
        # parameters are converted at this late stage
        # Get the output timezone (if any)
        self._tz = self._gettz()
        # Lines have already been create, set the tz
        self.lines.datetime._settz(self._tz)

        # This should probably be also called from an override-able method
        self._tzinput = bt.utils.date.Localizer(self._gettzinput())

        # Convert user input times to the output timezone (or min/max)
        if self.p.fromdate is None:
            self.fromdate = float('-inf')
        else:
            self.fromdate = self.date2num(self.p.fromdate)

        if self.p.todate is None:
            self.todate = float('inf')
        else:
            self.todate = self.date2num(self.p.todate)

        # FIXME: These two are never used and could be removed
        self.sessionstart = time2num(self.p.sessionstart)
        self.sessionend = time2num(self.p.sessionend)

    def _timeoffset(self):
        return self._tmoffset

    def _gettzinput(self):
        '''Can be overriden by classes to return a timezone for input'''
        return self.p.tzinput

    def _gettz(self):
        '''To be overriden by subclasses which may auto-calculate the
        timezone'''
        return bt.utils.date.Localizer(self.p.tz)

    def date2num(self, dt):
        if self._tz is not None:
            return date2num(self._tz.localize(dt))

        return date2num(dt)

    def num2date(self, dt=None, tz=None, naive=True):
        if dt is None:
            return num2date(self.lines.datetime[0], tz or self._tz, naive)

        return num2date(dt, tz or self._tz, naive)

    def islive(self):
        '''If this returns True, ``Cerebro`` will deactivate ``preload`` and
        ``runonce`` because a live data source must be fetched tick by tick (or
        bar by bar)'''
        return False

    def put_notification(self, status, *args, **kwargs):
        '''Add arguments to notification queue'''
        self.notifs.append((status, args, kwargs))
        self._laststatus = status

    def get_notifications(self):
        '''Return the pending "store" notifications'''
        # The background thread could keep on adding notifications. The None
        # mark allows to identify which is the last notification to deliver
        self.notifs.append(None)  # put a mark
        notifs = list()
        while True:
            notif = self.notifs.popleft()
            if notif is None:  # mark is reached
                break
            notifs.append(notif)

        return notifs

    def getfeed(self):
        return self._feed

    def qbuffer(self, savemem=0):
        extrasize = self.resampling
        for line in self.lines:
            line.qbuffer(savemem=savemem, extrasize=extrasize)

    def start(self):
        self._barstack = collections.deque()
        self.mlen = list()
        self._laststatus = self.CONNECTED

    def stop(self):
        pass

    def clone(self, **kwargs):
        return DataClone(dataname=self, **kwargs)

    def setenvironment(self, env):
        '''Keep a reference to the environment'''
        self._env = self

    def getenvironment(self):
        return self._env

    def addfilter_simple(self, f, *args, **kwargs):
        fp = SimpleFilterWrapper(self, f, *args, **kwargs)
        self._filters.append((fp, fp.args, fp.kwargs))

    def addfilter(self, p, *args, **kwargs):
        if inspect.isclass(p):
            pobj = p(self, *args, **kwargs)
            self._filters.append((pobj, [], {}))

            if hasattr(pobj, 'last'):
                self._ffilters.append((pobj, [], {}))

        else:
            self._filters.append((p, args, kwargs))

    def _tick_nullify(self):
        # These are the updating prices in case the new bar is "updated"
        # and the length doesn't change like if a replay is happening or
        # a real-time data feed is in use and 1 minutes bars are being
        # constructed with 5 seconds updates
        for lalias in self.getlinealiases():
            if lalias != 'datetime':
                setattr(self, 'tick_' + lalias, None)

        self.tick_last = None

    def _tick_fill(self, force=False):
        # If nothing filled the tick_xxx attributes, the bar is the tick
        alias0 = self._getlinealias(0)
        if force or getattr(self, 'tick_', alias0) is None:
            for lalias in self.getlinealiases():
                if lalias != 'datetime':
                    setattr(self, 'tick_' + lalias,
                            getattr(self.lines, lalias)[0])

            self.tick_last = getattr(self.lines, alias0)[0]

    def advance(self, size=1, datamaster=None):
        self._tick_nullify()

        # Need intercepting this call to support datas with
        # different lengths (timeframes)
        self.lines.advance(size)

        if datamaster is not None:
            if len(self) > self.buflen():
                # if no bar can be delivered, fill with an empty bar
                self.rewind()
                self.lines.forward()
                return

            if self.lines.datetime[0] > datamaster.lines.datetime[0]:
                self.lines.rewind()
            else:
                self.mlen.append(len(datamaster) - 1)
                self._tick_fill()
        elif len(self) < self.buflen():
            # a resampler may have advance us past the last point
            self._tick_fill()

    def next(self, datamaster=None):

        if len(self) >= self.buflen():
            self._tick_nullify()

            # not preloaded - request next bar
            ret = self.load()
            if not ret:
                # if load cannot produce bars - forward the result
                return ret

            if datamaster is None:
                # bar is there and no master ... return load's result
                self._tick_fill()
                return ret
        else:
            self.advance()

        # a bar is "loaded" or was preloaded - index has been moved to it
        if datamaster is not None:
            # there is a time reference to check against
            if self.lines.datetime[0] > datamaster.lines.datetime[0]:
                # can't deliver new bar, too early, go back
                self.rewind()
            else:
                self.mlen.append(len(datamaster) - 1)
                self._tick_fill()

        else:
            self._tick_fill()

        # tell the world there is a bar (either the new or the previous
        return True

    def preload(self):
        while self.load():
            pass

        self._last()
        self.home()

    def _last(self, datamaster=None):
        # Last chance for filters to deliver something
        ret = 0
        for ff, fargs, fkwargs in self._ffilters:
            ret += ff.last(self, *fargs, **fkwargs)

        if datamaster is not None and self._barstack:
            self.mlen.append(len(datamaster) - 1)
            self._tick_fill()

        while self._fromstack(forward=True):
            # consume bar(s) produced by "last"s - adding room
            pass

        return bool(ret)

    def _check(self):
        ret = 0
        for ff, fargs, fkwargs in self._filters:
            if not hasattr(ff, 'check'):
                continue
            ff.check(self, *fargs, **fkwargs)

    def load(self):
        while True:
            # move data pointer forward for new bar
            self.forward()

            if self._fromstack():  # bar is available
                return True

            _loadret = self._load()
            if not _loadret:  # no bar
                # use force to make sure in exactbars the pointer is undone
                # this covers especially (but not uniquely) the case in which
                # the last bar has been seen and a backwards would ruin pointer
                # accounting in the "stop" method of the strategy
                self.backwards(force=True)  # undo data pointer

                # return the actual returned value which may be None to signal
                # no bar is available, but the data feed is not done. False
                # means game over
                return _loadret

            # Get a reference to current loaded time
            dt = self.lines.datetime[0]

            # A bar has been loaded, adapt the time
            if self._tzinput:
                # Input has been converted at face value but it's not UTC in
                # the input stream
                dtime = num2date(dt)  # get it in a naive datetime
                # localize it
                dtime = self._tzinput.localize(dtime)  # pytz compatible-ized
                self.lines.datetime[0] = dt = date2num(dtime)  # keep UTC val

            # Check standard date from/to filters
            if dt < self.fromdate:
                # discard loaded bar and carry on
                self.backwards()
                continue
            if dt > self.todate:
                # discard loaded bar and break out
                self.backwards()
                break

            # Pass through filters
            retff = False
            for ff, fargs, fkwargs in self._filters:
                # previous filter may have put things onto the stack
                if self._barstack:
                    for i in range(len(self._barstack)):
                        self._fromstack(forward=True)
                        retff = ff(self, *fargs, **fkwargs)
                else:
                    retff = ff(self, *fargs, **fkwargs)

                if retff:  # bar removed from systemn
                    break  # out of the inner loop

            if retff:  # bar removed from system - loop to get new bar
                continue  # in the greater loop

            # Checks let the bar through ... notify it
            return True

        # Out of the loop ... no more bars or past todate
        return False

    def _load(self):
        return False

    def _add2stack(self, bar):
        '''Saves given bar (list of values) to the stack for later retrieval'''
        self._barstack.append(bar)

    def _save2stack(self, erase=False, force=False):
        '''Saves current bar to the bar stack for later retrieval

        Parameter ``erase`` determines removal from the data stream
        '''
        bar = [line[0] for line in self.itersize()]
        self._barstack.append(bar)

        if erase:  # remove bar if requested
            self.backwards(force=force)

    def _updatebar(self, bar, forward=False, ago=0):
        '''Load a value from the stack onto the lines to form the new bar

        Returns True if values are present, False otherwise
        '''
        if forward:
            self.forward()

        for line, val in zip(self.itersize(), bar):
            line[0 + ago] = val

    def _fromstack(self, forward=False):
        '''Load a value from the stack onto the lines to form the new bar

        Returns True if values are present, False otherwise
        '''
        if self._barstack:
            if forward:
                self.forward()

            for line, val in zip(self.itersize(), self._barstack.popleft()):
                line[0] = val

            return True

        return False

    def resample(self, **kwargs):
        self.addfilter(Resampler, **kwargs)

    def replay(self, **kwargs):
        self.addfilter(Replayer, **kwargs)


class DataBase(AbstractDataBase):
    pass


class FeedBase(with_metaclass(metabase.MetaParams, object)):
    params = () + DataBase.params._gettuple()

    def __init__(self):
        self.datas = list()

    def start(self):
        for data in self.datas:
            data.start()

    def stop(self):
        for data in self.datas:
            data.stop()

    def getdata(self, dataname, name=None, **kwargs):
        for pname, pvalue in self.p._getitems():
            kwargs.setdefault(pname, getattr(self.p, pname))

        kwargs['dataname'] = dataname
        data = self._getdata(**kwargs)

        data._name = name

        self.datas.append(data)
        return data

    def _getdata(self, dataname, **kwargs):
        for pname, pvalue in self.p._getitems():
            kwargs.setdefault(pname, getattr(self.p, pname))

        kwargs['dataname'] = dataname
        return self.DataCls(**kwargs)


class MetaCSVDataBase(DataBase.__class__):
    def dopostinit(cls, _obj, *args, **kwargs):
        _obj, args, kwargs = \
            super(MetaCSVDataBase, cls).dopostinit(_obj, *args, **kwargs)

        if not _obj._name:
            _obj._name, _ = os.path.splitext(os.path.basename(_obj.p.dataname))

        return _obj, args, kwargs


class CSVDataBase(with_metaclass(MetaCSVDataBase, DataBase)):
    '''
    Base class for classes implementing CSV DataFeeds

    The class takes care of opening the file, reading the lines and
    tokenizing them.

    Subclasses do only need to override:

      - _loadline(tokens)

    The return value of ``_loadline`` (True/False) will be the return value
    of ``_load`` which has been overriden by this base class
    '''

    f = None
    params = (('headers', True), ('separator', ','),)

    def start(self):
        super(CSVDataBase, self).start()

        if self.f is None:
            if hasattr(self.p.dataname, 'readline'):
                self.f = self.p.dataname
            else:
                # Let an exception propagate to let the caller know
                self.f = io.open(self.p.dataname, 'r')

        if self.p.headers:
            self.f.readline()  # skip the headers

        self.separator = self.p.separator

    def stop(self):
        super(CSVDataBase, self).stop()
        if self.f is not None:
            self.f.close()
            self.f = None

    def _load(self):
        if self.f is None:
            return False

        # Let an exception propagate to let the caller know
        line = self.f.readline()

        if not line:
            return False

        line = line.rstrip('\n')
        linetokens = line.split(self.separator)
        return self._loadline(linetokens)


class CSVFeedBase(FeedBase):
    params = (('basepath', ''),) + CSVDataBase.params._gettuple()

    def _getdata(self, dataname, **kwargs):
        return self.DataCls(dataname=self.p.basepath + dataname,
                            **self.p._getkwargs())


class DataClone(AbstractDataBase):

    def start(self):
        super(DataClone, self).start()
        self.data = self.p.dataname
        self._dlen = 0
        self._preloading = False

    def preload(self):
        self._preloading = True
        super(DataClone, self).preload()
        self.data.home()  # preloading data was pushed forward
        self._preloading = False

    def _load(self):
        # assumption: the data is in the system
        # simply copy the lines
        if self._preloading:
            # data is preloaded, we are preloading too, can move
            # forward until have full bar or data source is exhausted
            self.data.advance()
            if len(self.data) > self.data.buflen():
                return False

            for line, dline in zip(self.lines, self.data.lines):
                line[0] = dline[0]

            return True

        # Not preloading
        if not (len(self.data) > self._dlen):
            # Data not beyond last seen bar
            return False

        self._dlen += 1

        for line, dline in zip(self.lines, self.data.lines):
            line[0] = dline[0]

        return True
