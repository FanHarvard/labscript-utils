#####################################################################
#                                                                   #
# h5_lock.py                                                        #
#                                                                   #
# Copyright 2013, Monash University                                 #
#                                                                   #
# This file is part of the labscript suite (see                     #
# http://labscriptsuite.org) and is licensed under the Simplified   #
# BSD License. See the license.txt file in the root of the project  #
# for the full license.                                             #
#                                                                   #
#####################################################################
import errno
import logging
import sys
import os
import threading
import time

from labscript_utils.ls_zprocess import Lock, connect_to_zlock_server, kill_lock
from labscript_utils import dedent
from labscript_utils.shared_drive import path_to_agnostic

if 'h5py' in sys.modules:
    import labscript_utils.double_import_denier
    import h5py
    denier = labscript_utils.double_import_denier._denier
    if denier is not None and denier.enabled:
        tb = denier._format_tb(denier.tracebacks[os.path.dirname(h5py.__file__)])
        msg = """Error importing h5_lock: h5py has already been imported. h5_lock must
            be imported before any code imports h5py. The above traceback shows where
            h5_lock was imported, and the below traceback shows where h5py was imported
            prior."""
        msg = dedent(msg)
        msg += "\n\nTraceback (h5py import):\n"
        msg += "------------\n%s------------" % tb
        raise ImportError(msg)
    else:
        raise ImportError('h5_lock must be imported prior to importing h5py')
        
import h5py

_File = h5py.File

logger = logging.getLogger(__name__)

# On Windows, while another process has a file open without sharing write access, as
# copy and backup tools do, the file cannot be opened for writing, and the C runtime
# reports this as errno 13 (EACCES), the same as a real permission problem. A job that
# mirrors the experiment folders on the file server caused such refusals, and each one
# failed a shot and paused the BLACS queue (2026-11-01 run 0060 shot 37, 2026-11-04 run
# 0040 shot 5). Both were over within 0.65 s: the next opens of the same file for
# writing succeeded. So on Windows an open refused with errno 13, in any mode, is tried
# again after each of these pauses, 1.37 s in all, and the error is raised only if the
# last try is refused too. Every other error is raised at once. That includes HDF5's own
# "unable to lock file" even when it says errno 13: Windows file locking does not set
# errno, so HDF5 reports whatever errno the thread had last, which after a refused open
# is 13. Since 2026-11-04 the mirror job shares write access, so this retry is a second
# line of defence.
#
# The zlock and the kill lock stay held while waiting, so other labscript processes wait
# for the file in zlock's acquire(), which has no time limit, instead of meeting the same
# refusal. (Only reads next to a retried read go ahead, and retry on their own.) Nothing
# is waited for while sleeping and h5py's lock is not held, so this cannot deadlock, and
# the wait is far inside the 45 s zlock lease (ls_zprocess.ZLOCK_DEFAULT_TIMEOUT), the
# 300 s BLACS allows for transition_to_buffered, and the 5 s after which a tab shows
# "not responding".
if os.name == 'nt':
    _EACCES_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.6)
else:
    # Elsewhere errno 13 means a real permission problem, so it is not retried:
    _EACCES_RETRY_DELAYS = ()

# Named in the first record of each retried open, so that the logs show which version of
# this code was running:
_EACCES_RETRY_VERSION = '2026-11-07'

# Retries are also appended to this file in the profile's logs folder, since BLACS.log
# rotates within hours. The record that takes it past this size is followed by a line
# saying so, and nothing more is appended:
_RETRY_LOG_NAME = 'h5_open_retry.log'
_RETRY_LOG_MAX_BYTES = 10 * 1024 * 1024


def _retry_logger():
    """Return the logger that retries are reported to.

    setup_logging() gives the log file's handler to the program's own logger ('BLACS',
    'runmanager', 'lyse', ...), not to the root logger, so a record on this module's
    logger would only reach the terminal. BLACS, its workers and the other programs pass
    that logger to labscript_utils.excepthook.set_logger(), so use it when it is set.
    excepthook is only looked up here, never imported: importing it installs a Tk
    exception hook."""
    excepthook = sys.modules.get('labscript_utils.excepthook')
    program_logger = getattr(getattr(excepthook, 'l', None), 'logger', None)
    if program_logger is None:
        return logger
    return program_logger.getChild('h5_lock')


def _timestamp(now):
    """now, a time.time(), as the labscript logs write it: 2026-11-04 14:21:21,293"""
    return '%s,%03d' % (
        time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        int(now % 1 * 1000),
    )


def _write_retry_log(text, now):
    """Append a record to logs/h5_open_retry.log in the profile: the time now, pid,
    logger name and thread name, then text. Never raises: this is only a record."""
    try:
        from labscript_profile import LABSCRIPT_SUITE_PROFILE

        if LABSCRIPT_SUITE_PROFILE is None:
            return
        path = os.path.join(LABSCRIPT_SUITE_PROFILE, 'logs', _RETRY_LOG_NAME)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        if size > _RETRY_LOG_MAX_BYTES:
            return
        try:
            log_name = _retry_logger().name
        except Exception:
            log_name = logger.name
        prefix = '%s pid=%d %s %s ' % (
            _timestamp(now),
            os.getpid(),
            log_name,
            threading.current_thread().name,
        )
        data = (prefix + text + os.linesep).encode('utf-8', 'backslashreplace')
        if size + len(data) > _RETRY_LOG_MAX_BYTES:
            # Said once: from the next record on, the file is over the limit.
            data += (
                '%sthis file is now over %d bytes, so nothing more is added%s'
                % (prefix, _RETRY_LOG_MAX_BYTES, os.linesep)
            ).encode('utf-8', 'backslashreplace')
        # One write per record. The C runtime's append mode shares the file with other
        # writers and readers, so processes do not block each other.
        with open(path, 'ab') as f:
            f.write(data)
    except Exception:
        pass


def _log_retry_records(records):
    """Pass the (level, text) records of a retried open to the program's log. Never
    raises: this is only a record."""
    for level, text in records:
        try:
            _retry_logger().log(level, text)
        except Exception:
            pass


def _win32_open_error(name, mode):
    """Ask Windows directly whether the file opens as the C runtime opens it for this
    mode, and return the Win32 error: 32 is a sharing violation (another handle), 5
    access denied (read-only file, permissions, a folder, a pending delete), 2 or 3 not
    found, 0 means it opens by now. None if not on Windows. The file is never created:
    OPEN_EXISTING. A handle that opens is closed at once, and it asks for no more access
    or sharing than the open being retried."""
    if os.name != 'nt':
        return None
    import ctypes
    from ctypes import c_ulong, c_void_p, c_wchar_p

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateFileW.restype = c_void_p
    kernel32.CreateFileW.argtypes = (
        c_wchar_p, c_ulong, c_ulong, c_void_p, c_ulong, c_ulong, c_void_p
    )
    kernel32.CloseHandle.argtypes = (c_void_p,)
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    access = GENERIC_READ if mode == 'r' else GENERIC_READ | GENERIC_WRITE
    # FILE_SHARE_READ | FILE_SHARE_WRITE, as the C runtime shares; OPEN_EXISTING:
    handle = kernel32.CreateFileW(os.fsdecode(name), access, 3, None, 3, 0, None)
    if handle == c_void_p(-1).value:
        return ctypes.get_last_error()
    kernel32.CloseHandle(handle)
    return 0


def _probe(name, mode):
    """_win32_open_error() as text for a record."""
    try:
        return 'win32 error %s' % _win32_open_error(name, mode)
    except Exception as exc:
        return 'win32 probe failed: %r' % (exc,)


def _refused(exc):
    """Whether exc is an open refused with errno 13, which is retried. HDF5's "unable to
    lock file" is not, whatever errno its message gives."""
    return (
        isinstance(exc, OSError)
        and exc.errno == errno.EACCES
        and 'unable to lock file' not in str(exc).lower()
    )


def _open_retrying_eacces(open_file, name, mode, records):
    """Call open_file(). While it raises OSError with errno 13 (EACCES), call it again
    after each pause in _EACCES_RETRY_DELAYS, and if the last call is refused too, raise
    its error. Any other exception is raised at once.

    A retried open is recorded in logs/h5_open_retry.log as it happens. The same records,
    with the time of each, are appended to records as (level, text) pairs, which the
    caller passes to the program's log: if the open failed, after releasing the file's
    locks, because logging can block (a BLACS worker's stderr is a zmq socket to BLACS)
    while other processes wait for those locks; if it succeeded, at once, with the file
    open."""
    if not _EACCES_RETRY_DELAYS:
        open_file()
        return

    def record(level, message):
        now = time.time()
        try:
            text = 'errno 13 opening %s mode=%s: %s' % (name, mode, message)
            # The program's log gets this later, so it says when:
            program_text = 'errno 13 opening %s mode=%s at %s: %s' % (
                name,
                mode,
                _timestamp(now),
                message,
            )
        except Exception:
            return
        _write_retry_log(text, now)
        records.append((level, program_text))

    start = time.perf_counter()
    first_refusal = None
    refused_at = None
    for attempt, delay in enumerate(_EACCES_RETRY_DELAYS + (None,), 1):
        try:
            open_file()
        except Exception as exc:
            if not _refused(exc):
                if attempt > 1:
                    summary = (
                        'refused %d times over %.3f s (%s at the first), then failed '
                        'otherwise' % (attempt - 1, refused_at, first_refusal)
                    )
                    record(logging.ERROR, '%s: %r' % (summary, exc))
                    if hasattr(exc, 'add_note'):
                        exc.add_note('h5_lock: ' + summary)
                raise
            refused_at = time.perf_counter() - start
            if attempt == 1:
                first_refusal = _probe(name, mode)
                record(
                    logging.WARNING,
                    '%s; trying again for up to %.2f s (h5_lock retry %s): %s'
                    % (
                        first_refusal,
                        sum(_EACCES_RETRY_DELAYS),
                        _EACCES_RETRY_VERSION,
                        exc,
                    ),
                )
            if delay is None:
                summary = (
                    'refused %d times over %.3f s (%s at the first, %s at the last), '
                    'giving up' % (attempt, refused_at, first_refusal, _probe(name, mode))
                )
                record(logging.ERROR, summary)
                if hasattr(exc, 'add_note'):
                    exc.add_note('h5_lock: ' + summary)
                raise
            time.sleep(delay)
        else:
            if attempt > 1:
                record(
                    logging.WARNING,
                    'opened on attempt %d at %.3f s, last refused at %.3f s'
                    % (attempt, time.perf_counter() - start, refused_at),
                )
            return


class File(_File):
    def __init__(self, name, mode=None, driver=None, libver=None, **kwds):
        if not isinstance(name, h5py._objects.ObjectID):
            kwargs = {}
            if mode == 'r':
                kwargs['read_only'] = True
            # Do not terminate upon SIGTERM while the file is open:
            self.kill_lock = kill_lock
            self.kill_lock.acquire()
            # Ask other zlock users not to open the file while we have it open:
            self.zlock = Lock(path_to_agnostic(name), **kwargs)
            self.zlock.acquire()
        retry_records = []
        try:
            # h5py's File.__init__ sets nothing on this instance until make_fid() has
            # opened the file, so after a refused open it can simply be called again.
            _open_retrying_eacces(
                lambda: _File.__init__(self, name, mode, driver, libver, **kwds),
                name,
                mode,
                retry_records,
            )
        except:
            try:
                if hasattr(self, 'zlock'):
                    self.zlock.release()
                if hasattr(self, 'kill_lock'):
                    self.kill_lock.release()
            finally:
                _log_retry_records(retry_records)
            raise
        _log_retry_records(retry_records)

    def close(self):
        _File.close(self)
        if hasattr(self, 'zlock'):
            self.zlock.release()
        if hasattr(self, 'kill_lock'):
            self.kill_lock.release()

    # Overriding __exit__ is crucial. Since h5py.File.__exit__() holds h5py's
    # library-wide lock "phil", it calls close() whilst holding that lock. Our close()
    # method does not need the lock (h5py.File.close() does, but it acquires it itself
    # as needed), but this means they we're holding phil when we call
    # kill_lock.release(), which, in order to be thread-safe, attempts to aquire
    # kill_lock._lock, briefly. If another thread holds kill_lock._lock at the time, and
    # whilst holding it, Python garbage collection runs in that thread, and there are
    # HDF5 objects waiting to be deallocated, then Python deadlocks. This is beause the
    # deallocation in h5py is done while holding phil. But phil is held by our close()
    # method, so deallocation must wait for our close method to return. However our
    # close method is waiting to acquire kill_lock._lock, which will not be released by
    # the other thread until garbage collection is complete. Python hangs.
    #
    # The solution is just not not hold phil in __exit__. It does not appear to be
    # necessary. I will report this as an issue in h5py, and will remove this workaround
    # if it is fixed in a future version.
    def __exit__(self, *args):
        self.close()


def hack_locks_onto_h5py():
    # Monkeypatch h5py so all files are locked:
    h5py.File = File

if os.environ.get('READTHEDOCS'):
    # prevent starting a zlock server on RTD, which always fails
    pass
else:
    connect_to_zlock_server()
    hack_locks_onto_h5py()
