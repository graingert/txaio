###############################################################################
#
# The MIT License (MIT)
#
# Copyright (c) Tavendo GmbH
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#
###############################################################################

from __future__ import absolute_import, print_function

import sys
import time
import weakref
import functools
import traceback
import logging
from datetime import datetime

from txaio.interfaces import IFailedFuture, ILogger, log_levels
from txaio import _Config

import six

try:
    import asyncio
    from asyncio import iscoroutine
    from asyncio import Future

except ImportError:
    # Trollius >= 0.3 was renamed
    # noinspection PyUnresolvedReferences
    import trollius as asyncio
    from trollius import iscoroutine
    from trollius import Future

    class PrintHandler(logging.Handler):
        def emit(self, record):
            print(record)
    logging.getLogger("trollius").addHandler(PrintHandler())


config = _Config()
config.loop = asyncio.get_event_loop()
_stderr, _stdout = sys.stderr, sys.stdout
_loggers = []  # weak-references of each logger we've created before start_logging()
_log_level = 'info'  # re-set by start_logging

using_twisted = False
using_asyncio = True


class FailedFuture(IFailedFuture):
    """
    This provides an object with any features from Twisted's Failure
    that we might need in Autobahn classes that use FutureMixin.

    We need to encapsulate information from exceptions so that
    errbacks still have access to the traceback (in case they want to
    print it out) outside of "except" blocks.
    """

    def __init__(self, type_, value, traceback):
        """
        These are the same parameters as returned from ``sys.exc_info()``

        :param type_: exception type
        :param value: the Exception instance
        :param traceback: a traceback object
        """
        self._type = type_
        self._value = value
        self._traceback = traceback

    @property
    def value(self):
        return self._value

    def __str__(self):
        return str(self.value)


# API methods for txaio, exported via the top-level __init__.py

def _log(logger, level, msg, **kwargs):
    kwargs['log_time'] = time.time()
    kwargs['log_level'] = level
    kwargs['log_message'] = msg
    # NOTE: turning kwargs into a single "argument which
    # is a dict" on purpose, since a LogRecord only keeps
    # args, not kwargs.
    if level == 'trace':
        level = 'debug'
    getattr(logger._logger, level)(msg, kwargs)


def _no_op(*args, **kw):
    pass


class _TxaioLogWrapper(ILogger):
    def __init__(self, logger):
        self._logger = logger
        self._set_level(_log_level)

    def _set_level(self, level):
        target_level = log_levels.index(level)
        # this binds either _log or _no_op above to this instance,
        # depending on the desired level.
        for (idx, name) in enumerate(log_levels):
            if idx < target_level:
                log_method = functools.partial(_log, self, name)
            else:
                log_method = _no_op
            setattr(self, name, log_method)


class _TxaioFileHandler(logging.Handler):
    def __init__(self, fileobj, **kw):
        super(_TxaioFileHandler, self).__init__(**kw)
        self._file = fileobj

    def emit(self, record):
        fmt = record.args['log_message']
        dt = datetime.fromtimestamp(record.args['log_time'])
        msg = '{} {}\n'.format(
            dt.strftime("%Y-%m-%dT%H:%M:%S%z"),
            fmt.format(**record.args),
        )
        self._file.write(msg)


def make_logger():
    logger = _TxaioLogWrapper(logging.getLogger())
    # remember this so we can set their levels properly once
    # start_logging is actually called.
    if _loggers is not None:
        _loggers.append(weakref.ref(logger))
    return logger


def start_logging(out=None, level='info'):
    """
    Begin logging.

    :param out: if provided, a file-like object to log to
    :param level: the maximum log-level to emit (a string)
    """
    global _log_level, _loggers
    if level not in log_levels:
        raise RuntimeError(
            "Invalid log level '{}'; valid are: {}".format(
                level, ', '.join(log_levels)
            )
        )

    if _loggers is None:
        return
        raise RuntimeError("start_logging() may only be called once")
    _log_level = level

    if out is None:
        out = _stdout
    handler = _TxaioFileHandler(out)
    logging.getLogger().addHandler(handler)
    # note: Don't need to call basicConfig() or similar, because we've
    # now added at least one handler to the root logger
    logging.raiseExceptions = True  # FIXME
    level_to_stdlib = {
        'critical': logging.CRITICAL,
        'error': logging.ERROR,
        'warn': logging.WARNING,
        'info': logging.INFO,
        'debug': logging.DEBUG,
        'trace': logging.DEBUG,
    }
    logging.getLogger().setLevel(level_to_stdlib[level])
    # make sure any loggers we created before now have their log-level
    # set (any created after now will get it from _log_level
    for ref in _loggers:
        instance = ref()
        if instance is not None:
            instance._set_level(level)
    _loggers = None


def failure_message(fail):
    """
    :param fail: must be an IFailedFuture
    returns a unicode error-message
    """
    return '{}: {}'.format(fail._value.__class__.__name__, str(fail._value))


def failure_traceback(fail):
    """
    :param fail: must be an IFailedFuture
    returns a traceback instance
    """
    return fail._traceback


def failure_format_traceback(fail):
    """
    :param fail: must be an IFailedFuture
    returns a string
    """
    f = six.StringIO()
    traceback.print_exception(
        fail._type,
        fail.value,
        fail._traceback,
        file=f,
    )
    return f.getvalue()


_unspecified = object()


def create_future(result=_unspecified, error=_unspecified):
    if result is not _unspecified and error is not _unspecified:
        raise ValueError("Cannot have both result and error.")

    f = Future()
    if result is not _unspecified:
        resolve(f, result)
    elif error is not _unspecified:
        reject(f, error)
    return f


def create_future_success(result):
    return create_future(result=result)


def create_future_error(error=None):
    f = create_future()
    reject(f, error)
    return f


# XXX maybe rename to call()?
def as_future(fun, *args, **kwargs):
    try:
        res = fun(*args, **kwargs)
    except Exception:
        return create_future_error(create_failure())
    else:
        if isinstance(res, Future):
            return res
        elif iscoroutine(res):
            return asyncio.Task(res)
        else:
            return create_future_success(res)


def call_later(delay, fun, *args, **kwargs):
    # loop.call_later doesns't support kwargs
    real_call = functools.partial(fun, *args, **kwargs)
    return config.loop.call_later(delay, real_call)


def resolve(future, result=None):
    future.set_result(result)


def reject(future, error=None):
    if error is None:
        error = create_failure()  # will be error if we're not in an "except"
    elif isinstance(error, Exception):
        error = FailedFuture(type(error), error, None)
    else:
        if not isinstance(error, IFailedFuture):
            raise RuntimeError("reject requires an IFailedFuture or Exception")
    future.set_exception(error.value)


def create_failure(exception=None):
    """
    This returns an object implementing IFailedFuture.

    If exception is None (the default) we MUST be called within an
    "except" block (such that sys.exc_info() returns useful
    information).
    """
    if exception:
        return FailedFuture(type(exception), exception, None)
    return FailedFuture(*sys.exc_info())


def add_callbacks(future, callback, errback):
    """
    callback or errback may be None, but at least one must be
    non-None.

    XXX beware the "f._result" hack to get "chainable-callback" type
    behavior.
    """
    def done(f):
        try:
            res = f.result()
            if callback:
                x = callback(res)
                if x is not None:
                    f._result = x
        except Exception:
            if errback:
                errback(create_failure())
    return future.add_done_callback(done)


def gather(futures, consume_exceptions=True):
    """
    This returns a Future that waits for all the Futures in the list
    ``futures``

    :param futures: a list of Futures (or coroutines?)

    :param consume_exceptions: if True, any errors are eaten and
    returned in the result list.
    """

    # from the asyncio docs: "If return_exceptions is True, exceptions
    # in the tasks are treated the same as successful results, and
    # gathered in the result list; otherwise, the first raised
    # exception will be immediately propagated to the returned
    # future."
    return asyncio.gather(*futures, return_exceptions=consume_exceptions)