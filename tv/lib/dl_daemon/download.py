# Miro - an RSS based video player application
# Copyright (C) 2005, 2006, 2007, 2008, 2009, 2010, 2011
# Participatory Culture Foundation
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301 USA
#
# In addition, as a special exception, the copyright holders give
# permission to link the code of portions of this program with the OpenSSL
# library.
#
# You must obey the GNU General Public License in all respects for all of
# the code used other than OpenSSL. If you modify file(s) with this
# exception, you may extend this exception to your version of the file(s),
# but you are not obligated to do so. If you do not wish to do so, delete
# this exception statement from your version. If you delete this exception
# statement from all source files in the program, then also delete it here.

import os
import stat
import time
from threading import RLock
from copy import copy
import sys
import datetime
import logging
import tempfile
import base64

from miro.gtcache import gettext as _

import libtorrent as lt
from miro.clock import clock
from miro.download_utils import (
    clean_filename, next_free_filename, next_free_directory,
    check_filename_extension, filter_directory_name, filename_from_url,
    get_file_url_path)
from miro import eventloop
from miro import httpclient
from miro import fileutil

from miro import app
from miro import prefs

from miro.dl_daemon import command
from miro.dl_daemon import daemon
from miro.util import (
    check_f, check_u, stringify, MAX_TORRENT_SIZE, returns_filename,
    info_hash_from_magnet, is_magnet_uri)
from miro.plat.utils import (
    get_available_bytes_for_movies, utf8_to_filename, PlatformFilenameType)

# Don't remove - it is used for unit tests.
chatter = True

# a hash of download ids to downloaders
_downloads = {}

_lock = RLock()

def create_downloader(url, content_type, dlid, magnet=None):
    """Creates a downloader based on the content_type.
    """
    check_u(url)
    check_u(content_type)
    if content_type == u'application/x-bittorrent':
        return BTDownloader(url, dlid)
    elif content_type ==  u'application/x-magnet':
        return BTDownloader(None, dlid, magnet=url)
    else:
        return HTTPDownloader(url, dlid, expectedContentType=content_type)

def pause_download(dlid):
    """Pauses a download by download id.

    :returns: True if there is no download with this id or whatever
        the downloader.pause() returns (which is None)
    """
    try:
        download = _downloads[dlid]
    except KeyError:
        # There is no download with this id
        return True
    return download.pause()

def info_hash_to_long(info_hash):
    """The info_hash() method from libtorrent returns a "big_number" object.
    This doesn't hash very well: different instances with the same value
    will have different hashes.  So we need to convert them to long objects,
    though this weird process.
    """
    return long(str(info_hash), 16)

def start_download(url, dlid, content_type, channel_name):
    try:
        download = _downloads[dlid]
        download.start()
    except KeyError:
        # There is no download with this id.  This is a new download.
        check_u(url)
        check_u(content_type)
        if channel_name:
            check_f(channel_name)
        dl = create_downloader(url, content_type, dlid)
        dl.channelName = channel_name
        _downloads[dlid] = dl

def stop_download(dlid, delete):
    _lock.acquire()
    try:
        download = _downloads[dlid]
        del _downloads[dlid]
    except KeyError:
        # There is no download with this id
        return True
    finally:
        _lock.release()
    return download.stop(delete)

def stop_upload(dlid):
    _lock.acquire()
    try:
        download = _downloads[dlid]
        if download.state not in (u"uploading", u"uploading-paused"):
            return
        del _downloads[dlid]
    except KeyError:
        # There is no download with this id
        return
    finally:
        _lock.release()
    return download.stop_upload()

def pause_upload(dlid):
    _lock.acquire()
    try:
        download = _downloads[dlid]
        if download.state != u"uploading":
            return
        del _downloads[dlid]
    except KeyError:
        # There is no download with this id
        return
    finally:
        _lock.release()
    return download.pause_upload()

def migrate_download(dlid, directory):
    check_f(directory)
    try:
        download = _downloads[dlid]
    except KeyError:
        # There is no download with this id
        return

    if download.state in (u"finished", u"uploading", u"uploading-paused"):
        download.move_to_directory(directory)

def get_download_status(dlids=None):
    statuses = {}
    for key in _downloads.keys():
        if dlids is None or dlids == key or key in dlids:
            try:
                statuses[key] = _downloads[key].get_status()
            except KeyError:
                pass
    return statuses

def startup():
    logging.info("Starting downloaders")
    DOWNLOAD_UPDATER.start_updates()
    TORRENT_SESSION.startup()

def shutdown():
    logging.info("Shutting down downloaders...")
    for dlid in _downloads:
        _downloads[dlid].shutdown()
    logging.info("Shutting down torrent session...")
    TORRENT_SESSION.shutdown()
    # Flush the status updates.
    logging.info('flushing status updates...')
    DOWNLOAD_UPDATER.flush_update()
    logging.info("shutdown() finished")

def restore_downloader(downloader):
    if downloader['dlid'] in _downloads:
        logging.warn("Not restarting active downloader: %s",
                downloader['dlid'])
        return

    downloader = copy(downloader)
    dler_type = downloader.get('dlerType')
    if dler_type == u'HTTP':
        dl = HTTPDownloader(restore=downloader)
    elif dler_type == u'BitTorrent':
        dl = BTDownloader(restore=downloader)
    else:
        err = u"in restore_downloader(): unknown dlerType: %s" % dler_type
        c = command.DownloaderErrorCommand(daemon.LAST_DAEMON, err)
        c.send()
        return

    _downloads[downloader['dlid']] = dl

class TorrentSession(object):
    """Contains the bittorrent session and handles updating all
    running bittorrents.
    """
    def __init__(self):
        self.torrents = set()
        self.info_hash_to_downloader = {}
        self.session = None
        self.pnp_on = None
        self.dht_on = None
        self.pe_set = None
        self.enc_req = None

    def startup(self):
        version = app.config.get(prefs.APP_VERSION).split(".")
        try:
            major = int(version[0])
        except ValueError:
            logging.exception("major version is not an int!")
            # FIXME - this is arbitrary
            major = 1
        try:
            minor = int(version[1])
        except (ValueError, IndexError):
            minor = 0

        # MR is for Miro.
        fingerprint = lt.fingerprint("MR", major, minor, 0, 0)
        self.session = lt.session(fingerprint)
        self.listen()
        self.set_upnp()
        self.set_dht()
        self.set_upload_limit()
        self.set_download_limit()
        self.set_encryption()
        self.callback_handle = app.downloader_config_watcher.connect('changed',
                self.on_config_changed)

    def listen(self):
        self.session.listen_on(app.config.get(prefs.BT_MIN_PORT),
                               app.config.get(prefs.BT_MAX_PORT))

    def set_upnp(self):
        use_upnp = app.config.get(prefs.USE_UPNP)
        if use_upnp == self.pnp_on:
            return
        self.pnp_on = use_upnp
        if use_upnp:
            self.session.start_upnp()
        else:
            self.session.stop_upnp()

    def set_dht(self):
        use_dht = app.config.get(prefs.USE_DHT)
        if use_dht == self.dht_on:
            return
        self.dht_on = use_dht
        if use_dht:
            self.session.start_dht(None)
            self.session.add_dht_router("router.bittorrent.com", 6881)
            self.session.add_dht_router("router.utorrent.com", 6881)
            self.session.add_dht_router("router.bitcomet.com", 6881)
            self.session.add_dht_router("dht.transmissionbt.com", 6881)
            self.session.add_dht_router("dht.aelitis.com ", 6881) # Vuze
        else:
            self.session.stop_dht()

    def set_upload_limit(self):
        limit = -1
        if app.config.get(prefs.LIMIT_UPSTREAM):
            limit = app.config.get(prefs.UPSTREAM_LIMIT_IN_KBS)
            limit = limit * (2 ** 10)
            if limit > sys.maxint:
                # avoid OverflowErrors by keeping the value an integer
                limit = sys.maxint
        self.session.set_upload_rate_limit(limit)

    def set_download_limit(self):
        limit = -1
        if app.config.get(prefs.LIMIT_DOWNSTREAM_BT):
            limit = app.config.get(prefs.DOWNSTREAM_BT_LIMIT_IN_KBS)
            limit = limit * (2 ** 10)
            if limit > sys.maxint:
                # avoid OverflowErrors by keeping the value an integer
                limit = sys.maxint
        self.session.set_download_rate_limit(limit)

    def set_connection_limit(self):
        limit = -1
        if app.config.get(prefs.LIMIT_CONNECTIONS_BT):
            limit = app.config.get(prefs.CONNECTION_LIMIT_BT_NUM)
            if limit > 65536:
                # there are only 2**16 TCP port numbers
                limit = 65536
        self.session.set_max_connections(limit)

    def set_encryption(self):
        if self.pe_set is None:
            self.pe_set = lt.pe_settings()
        enc_req = app.config.get(prefs.BT_ENC_REQ)
        if enc_req != self.enc_req:
            self.enc_req = enc_req
            if enc_req:
                self.pe_set.in_enc_policy = lt.enc_policy.forced
                self.pe_set.out_enc_policy = lt.enc_policy.forced
            else:
                self.pe_set.in_enc_policy = lt.enc_policy.enabled
                self.pe_set.out_enc_policy = lt.enc_policy.enabled
            self.session.set_pe_settings(self.pe_set)

    def shutdown(self):
        self.session.stop_upnp()
        self.session.stop_dht()
        app.downloader_config_watcher.disconnect(self.callback_handle)

    def on_config_changed(self, obj, key, value):
        if key == prefs.BT_MIN_PORT.key:
            if value > self.session.listen_port():
                self.listen()
        elif key == prefs.BT_MAX_PORT.key:
            if value < self.session.listen_port():
                self.listen()
        elif key == prefs.USE_UPNP.key:
            self.set_upnp()
        elif key == prefs.USE_DHT.key:
            self.set_dht()
        elif key in (prefs.LIMIT_UPSTREAM.key,
                     prefs.UPSTREAM_LIMIT_IN_KBS.key):
            self.set_upload_limit()
        elif key in (prefs.LIMIT_DOWNSTREAM_BT.key,
                     prefs.DOWNSTREAM_BT_LIMIT_IN_KBS.key):
            self.set_download_limit()
        elif key == prefs.BT_ENC_REQ.key:
            self.set_encryption()
        elif key in (prefs.LIMIT_CONNECTIONS_BT.key,
                     prefs.CONNECTION_LIMIT_BT_NUM.key):
            self.set_connection_limit()

    def find_duplicate_torrent(self, torrent_info):
        info_hash = info_hash_to_long(torrent_info.info_hash())
        return self.info_hash_to_downloader.get(info_hash)

    def find_duplicate_torrent_from_magnet(self, magnet):
        info_hash = info_hash_from_magnet(magnet)
        # If the magnet link does not have an info hash we skip finding
        # duplicates
        if not info_hash:
            return None
        # There are two possibilities here: it is either a base32 encoded
        # or a hex encoded hash. If it is base32 we have to change it
        # to the hex encoding. base32 info hashes should have a length
        # of 32, and hex info hashes a length of 40.
        # See #16794 for more info.
        if len(info_hash) == 32:
             info_hash = base64.b32decode(info_hash)
             info_hash = base64.b16encode(info_hash)
        info_hash = info_hash_to_long(info_hash)
        return self.info_hash_to_downloader.get(info_hash)

    def add_torrent(self, downloader):
        self.torrents.add(downloader)
        info_hash = info_hash_to_long(downloader.torrent.info_hash())
        self.info_hash_to_downloader[info_hash] = downloader

    def remove_torrent(self, downloader):
        if downloader in self.torrents:
            self.torrents.remove(downloader)
            info_hash = info_hash_to_long(downloader.torrent.info_hash())
            del self.info_hash_to_downloader[info_hash]

    def update_torrents(self):
        # Copy this set into a list in case any of the torrents gets
        # removed during the iteration.
        for torrent in [x for x in self.torrents]:
            torrent.update_status()

TORRENT_SESSION = TorrentSession()

class DownloadStatusUpdater(object):
    """Handles updating status for all in progress downloaders.

    On OS X and gtk if the user is on the downloads page and has a
    bunch of downloads going, this can be a fairly CPU intensive task.
    DownloadStatusUpdaters mitigate this in 2 ways.

    1. DownloadStatusUpdater objects batch all status updates into one
       big update which takes much less CPU.

    2. The update don't happen fairly infrequently (currently every 5
       seconds).

    Because updates happen infrequently, DownloadStatusUpdaters should
    only be used for progress updates, not events like downloads
    starting/finishing.  For those just call update_client() since
    they are more urgent, and don't happen often enough to cause CPU
    problems.
    """

    UPDATE_CLIENT_INTERVAL = 1

    def __init__(self):
        self.to_update = set()
        self.cmds_done = False

    def start_updates(self):
        eventloop.add_timeout(self.UPDATE_CLIENT_INTERVAL, self.do_update,
                "Download status update")

    def flush_update(self):
        self.do_update(periodic=False)

    def do_update(self, periodic=True):
        try:
            TORRENT_SESSION.update_torrents()
            statuses = []
            for downloader in self.to_update:
                statuses.append(downloader.get_status())
            self.to_update = set()
            if statuses or self.cmds_done:
                command.BatchUpdateDownloadStatus(daemon.LAST_DAEMON,
                                                  statuses,
                                                  self.cmds_done).send()
                self.cmds_done = False
        finally:
            if periodic:
                eventloop.add_timeout(self.UPDATE_CLIENT_INTERVAL,
                                      self.do_update,
                                      "Download status update")

    def set_cmds_done(self):
        self.cmds_done = True

    def queue_update(self, downloader):
        self.to_update.add(downloader)

DOWNLOAD_UPDATER = DownloadStatusUpdater()

# retry times in seconds.  60 seconds, 5 minutes, ...
RETRY_TIMES = (
    60,
    5 * 60,
    10 * 60,
    30 * 60,
    60 * 60,
    2 * 60 * 60,
    6 * 60 * 60,
    24 * 60 * 60
    )

class BGDownloader(object):
    def __init__(self, url, dlid):
        self.dlid = dlid
        self.url = url
        self.startTime = clock()
        self.endTime = self.startTime
        self.shortFilename = filename_from_url(url)
        self.pick_initial_filename()
        self.state = u"downloading"
        self.currentSize = 0
        self.totalSize = -1
        self.shortReasonFailed = self.reasonFailed = u"No Error"
        self.retryTime = None
        self.retryCount = -1

    def get_url(self):
        return self.url

    def get_status(self):
        return {'dlid': self.dlid,
            'url': self.url,
            'state': self.state,
            'totalSize': self.totalSize,
            'currentSize': self.currentSize,
            'eta': self.get_eta(),
            'rate': self.get_rate(),
            'uploaded': 0,
            'filename': self.filename,
            'startTime': self.startTime,
            'endTime': self.endTime,
            'shortFilename': self.shortFilename,
            'reasonFailed': self.reasonFailed,
            'shortReasonFailed': self.shortReasonFailed,
            'dlerType': None,
            'retryTime': self.retryTime,
            'retryCount': self.retryCount,
            'channelName': self.channelName}

    def update_client(self, now=False):
        if not now:
            DOWNLOAD_UPDATER.queue_update(self)
        else:
            command.BatchUpdateDownloadStatus(daemon.LAST_DAEMON,
                                              [self.get_status()]).send()

    def pick_initial_filename(self, suffix=".part", torrent=False,
                              is_directory=False):
        """Pick a path to download to based on self.shortFilename.

        This method sets self.filename, as well as creates any leading
        paths needed to start downloading there.

        :param torrent: If True, then the filename we're working on is
            encoded in utf-8 and shouldn't be transformed in any way.
            If False, then the filanem we're working on is encoded in
            ascii and needs to be transformed into something sane.
        :param is_directory: If True, we're really creating a
            directory--not a file.
        """
        download_dir = os.path.join(app.config.get(prefs.MOVIES_DIRECTORY),
                                    'Incomplete Downloads')
        # Create the download directory if it doesn't already exist.
        if not os.path.exists(download_dir):
            fileutil.makedirs(download_dir)
        filename = self.shortFilename + suffix
        if not torrent:
            # this is an ascii filename and needs to be fixed
            filename = clean_filename(filename)

        full_path = os.path.join(download_dir, filename)
        if is_directory:
            # if this is a torrent and it's a directory of files, then
            # we create a temp directory to put the directory of files
            # in.
            new_filename = next_free_directory(full_path)
        else:
            new_filename, fp = next_free_filename(full_path)
            fp.close()
        self.filename = new_filename

    def move_to_movies_directory(self):
        """Move our downloaded file from the Incomplete Downloads
        directory to the movies directory.
        """
        if chatter:
            logging.debug("move_to_movies_directory: filename is %s",
                          self.filename)
        self.move_to_directory(app.config.get(prefs.MOVIES_DIRECTORY))

    def _ensure_directory_exists(self, directory):
        """Make sure that a directory path exists.

        Under certain weird conditions, this function may need to choose a
        different directory name to make things work.  We return that
        directory name.

        :returns: path of a directory that we can put files in
        """

        directory_base = directory
        i = 1
        while True:
            if os.path.exists(directory):
                return directory
            try:
                fileutil.makedirs(directory)
            except OSError, e:
                if e.errno != 13:
                    raise # only handle permission denied
                # this weirdness happens on windows when the directory is
                # scheduled for deletion, but has a file handle open for it.
                # (#17456)
                directory = "%s.%s" % (directory_base, i)
                i += 1
            else:
                return directory

    def move_to_directory(self, directory):
        check_f(directory)
        if self.channelName:
            channel_name = filter_directory_name(self.channelName)
            # bug 10769: shutil and windows has problems with long
            # filenames, so we clip the directory name.
            if len(channel_name) > 80:
                channel_name = channel_name[:80]
            directory = os.path.join(directory, channel_name)
        directory = self._ensure_directory_exists(directory)

        src = self.filename
        dest = os.path.join(directory, self.shortFilename)
        if src == dest:
            return

        try:
            is_dir = os.path.isdir(src)
            if is_dir:
                dest = next_free_directory(dest)
            else:
                dest, fp = next_free_filename(dest)
                fp.close()
        except ValueError:
            func = 'next_free_directory' if is_dir else 'next_free_filename'
            logging.warn('move_to_directory: %s failed.  candidate = %r',
                         func, dest)
            return

        def callback():
            # for torrent of a directory of files, we want to remove
            # the temp directory we created in Incomplete Downloads
            # because we don't need it anymore.
            if os.path.isdir(self.filename):
                try:
                    fileutil.rmtree(self.filename)
                except OSError:
                    pass
            self.filename = dest
            self.update_client()

        fileutil.migrate_file(src, dest, callback)

    def get_eta(self):
        """Returns a float with the estimated number of seconds left.
        """
        if self.totalSize == -1:
            return -1
        rate = self.get_rate()
        if rate > 0:
            return (self.totalSize - self.currentSize) / rate
        else:
            return 0

    def get_rate(self):
        """Returns a float with the download rate in bytes per second
        """
        if self.endTime != self.startTime:
            rate = self.currentSize / (self.endTime - self.startTime)
        else:
            rate = self.rate
        return rate

    def retry_download(self):
        self.retryDC = None
        self.start(resume=False)

    def handle_temporary_error(self, short_reason, reason):
        self.state = u"offline"
        self.endTime = self.startTime = 0
        self.rate = 0
        self.reasonFailed = reason
        self.shortReasonFailed = short_reason
        self.retryCount = self.retryCount + 1
        if self.retryCount >= len(RETRY_TIMES):
            self.retryCount = len(RETRY_TIMES) - 1
        self.retryDC = eventloop.add_timeout(
            RETRY_TIMES[self.retryCount], self.retry_download,
            "Logarithmic retry")
        now = datetime.datetime.now()
        self.retryTime = now + datetime.timedelta(
            seconds=RETRY_TIMES[self.retryCount])
        logging.warning("Temporary error: '%s' '%s'.  retrying at %s %s",
                        short_reason, reason, self.retryTime, self.retryCount)
        self.update_client()

    def handle_error(self, short_reason, reason):
        self.state = u"failed"
        self.reasonFailed = reason
        self.shortReasonFailed = short_reason
        self.update_client()

    def handle_network_error(self, error):
        if isinstance(error, httpclient.NetworkError):
            if (isinstance(error, (httpclient.MalformedURL,
                                   httpclient.UnknownHostError,
                                   httpclient.AuthorizationFailed,
                                   httpclient.ProxyAuthorizationFailed,
                                   httpclient.UnexpectedStatusCode))):
                self.handle_error(error.getFriendlyDescription(),
                                  error.getLongDescription())
                self.retryCount = -1 # reset retryCount
            else:
                self.handle_temporary_error(error.getFriendlyDescription(),
                                            error.getLongDescription())
        else:
            logging.warning("grab_url errback not called with "
                            "NetworkError")
            self.handle_error(str(error), str(error))

    def handle_generic_error(self, longDescription):
        self.handle_error(_("Error"), longDescription)

    def accept_download_size(self, size):
        """Checks the download file size to see if we can accept it
        based on the user disk space preservation preference
        """
        accept = True
        if app.config.get(prefs.PRESERVE_DISK_SPACE):
            if size < 0:
                size = 0
            preserved = (app.config.get(prefs.PRESERVE_X_GB_FREE) *
                         1024 * 1024 * 1024)
            available = get_available_bytes_for_movies() - preserved
            accept = (size <= available)
        return accept

class HTTPDownloader(BGDownloader):
    CHECK_STATS_TIMEOUT = 1.0

    def __init__(self, url=None, dlid=None, restore=None,
                 expectedContentType=None):
        self.retryDC = None
        self.channelName = None
        self.expectedContentType = expectedContentType
        if restore is not None:
            if not isinstance(restore.get('totalSize', 0), int):
                # Sometimes restoring old downloaders caused errors
                # because their totalSize wasn't an int.  (see #3965)
                restore['totalSize'] = int(restore['totalSize'])
            self.__dict__.update(restore)
            self.restartOnError = True
        else:
            BGDownloader.__init__(self, url, dlid)
            self.restartOnError = False
        self.client = None
        self.rate = 0
        if self.state == u'downloading':
            self.start_download()
        elif self.state == u'offline':
            self.start()
        else:
            self.update_client()

    def start_new_download(self):
        """Start a download, discarding any existing data"""
        self.currentSize = 0
        self.totalSize = -1
        self.start_download(resume=False)

    def start_download(self, resume=True):
        if self.retryDC:
            self.retryDC.cancel()
            self.retryDC = None
        if resume:
            resume = self._resume_sanity_check()

        logging.debug("start_download: %s", self.url)

        self.client = httpclient.grab_url(
            self.url, self.on_download_finished, self.on_download_error,
            header_callback=self.on_headers, write_file=self.filename,
            resume=resume)
        self.update_stats()

    def _resume_sanity_check(self):
        """Do sanity checks to test if we should try HTTP Resume.

        :returns: If we should still try HTTP resume
        """
        if not os.path.exists(self.filename):
            return False
        # sanity check that the file we're resuming from is the right
        # size.  In particular, before the libcurl change, we would
        # preallocate the entire file, so we need to undo this.
        file_size = os.stat(self.filename)[stat.ST_SIZE]
        if file_size > self.currentSize:
            # use logging.info rather than warn, since this is the
            # usual case from upgrading from 3.0.x to 3.1
            logging.info("File larger than currentSize: truncating.  "
                         "url: %s, path: %s.", self.url, self.filename)
            f = open(self.filename, "ab")
            f.truncate(self.currentSize)
            f.close()
        elif file_size < self.currentSize:
            # Data got deleted somehow.  Let's start over.
            logging.warn("File doesn't contain enough data to resume.  "
                    "url: %s, path: %s.", self.url, self.filename)
            return False
        return True

    def destroy_client(self):
        """update the stats before we throw away the client.
        """
        self.update_stats()
        self.client = None

    def cancel_request(self, remove_file=False):
        if self.client is not None:
            self.client.cancel(remove_file=remove_file)
            self.destroy_client()
        # if it's in a retrying state, we want to nix that, too
        if self.retryDC:
            self.retryDC.cancel()
            self.retryDC = None

    def handle_error(self, short_reason, reason):
        BGDownloader.handle_error(self, short_reason, reason)
        self.cancel_request()
        if os.path.exists(self.filename):
            try:
                fileutil.remove(self.filename)
            except OSError:
                pass
        self.currentSize = 0
        self.totalSize = -1

    def handle_temporary_error(self, short_reason, reason):
        self.cancel_request()
        BGDownloader.handle_temporary_error(self, short_reason, reason)

    def handle_move_error(self, error):
        logging.exception("Error moving to movies directory\n"
                "filename: %s, shortFilename: %s, movies directory: %s",
                self.filename, self.shortFilename,
            app.config.get(prefs.MOVIES_DIRECTORY))
        text = _("Error moving to movies directory")
        self.handle_generic_error(text)

    def on_headers(self, info):
        if 'total-size' in info:
            self.totalSize = info['total-size']
        if not self.accept_download_size(self.totalSize):
            self.handle_error(_("Not enough disk space"),
                _("%(amount)s MB required to store this video",
                  {"amount": self.totalSize / (2 ** 20)}))
            return
        # We should successfully download the file.  Reset retryCount
        # and accept defeat if we see an error.
        self.restartOnError = False
        # update shortFilename based on the headers.  This will affect
        # how we move the file once the download is finished
        self.shortFilename = clean_filename(info['filename'])
        if self.expectedContentType is not None:
            ext_content_type = self.expectedContentType
        else:
            ext_content_type = info.get('content-type')
        self.shortFilename = check_filename_extension(self.shortFilename,
                ext_content_type)

    def on_download_error(self, error):
        if isinstance(error, httpclient.ResumeFailed):
            # try starting from scratch
            self.currentSize = 0
            self.totalSize = -1
            self.start_new_download()
        elif isinstance(error, httpclient.AuthorizationCanceled):
            self.destroy_client()
            self.stop(False)
        elif self.restartOnError:
            self.restartOnError = False
            self.start_download()
        else:
            self.destroy_client()
            self.handle_network_error(error)

    def on_download_finished(self, response):
        self.destroy_client()
        self.state = u"finished"
        self.endTime = clock()
        # bug 14131 -- if there's nothing here, treat it like a temporary
        # error
        if self.currentSize == 0:
            self.handle_network_error(httpclient.PossiblyTemporaryError(
                _("no content")))

        else:
            if self.totalSize == -1:
                self.totalSize = self.currentSize
            try:
                self.move_to_movies_directory()
            except (OSError, IOError), e:
                self.handle_move_error(e)
        self.update_client()

    def get_status(self):
        data = BGDownloader.get_status(self)
        data['dlerType'] = 'HTTP'
        return data

    def update_stats(self):
        """Update the download rate and eta based on receiving length
        bytes.
        """
        if self.client is None or self.state != u'downloading':
            return
        stats = self.client.get_stats()
        if stats.status_code in (200, 206):
            # Only upload currentSize/rate if we are currently
            # downloading something.  Don't change them before the
            # transfer starts, while we are handling redirects, etc.
            self.currentSize = stats.downloaded + stats.initial_size
            self.rate = stats.download_rate
        eventloop.add_timeout(self.CHECK_STATS_TIMEOUT, self.update_stats,
                'update http downloader stats')
        self.update_client()

    def pause(self):
        """Pauses the download.
        """
        if self.state != u"stopped":
            self.cancel_request()
            self.state = u"paused"
            self.update_client()

    def stop(self, delete):
        """Stops the download and removes the partially downloaded
        file.
        """
        if self.state == u'finished':
            if delete:
                try:
                    if fileutil.isdir(self.filename):
                        fileutil.rmtree(self.filename)
                    else:
                        fileutil.remove(self.filename)
                except OSError:
                    pass
        else:
            # Cancel the request, don't keep around partially
            # downloaded data
            self.cancel_request(remove_file=True)
        self.currentSize = 0
        self.state = u"stopped"
        self.update_client()

    def stop_upload(self):
        # HTTP downloads never upload.
        pass

    def start(self, resume=True):
        """Continues a paused or stopped download thread.
        """
        if self.state in (u'paused', u'stopped', u'offline'):
            self.state = u"downloading"
            self.start_download(resume=resume)

    def shutdown(self):
        self.cancel_request()
        self.update_client()


@returns_filename
def generate_fast_resume_filename(info_hash):
    filename = PlatformFilenameType(clean_filename(info_hash) + ".fastresume")

    support_dir = app.config.get(prefs.SUPPORT_DIRECTORY)
    fast_resume_file = os.path.join(support_dir, 'fastresume', filename)

    return fast_resume_file

def save_fast_resume_data(info_hash, fast_resume_data):
    """Saves fast_resume_data to disk.

    If it encounters problems, then it prints something to the log
    and otherwise eats the exceptions.

    :param info_hash: the torrent handle info hash--this is unique to
        a torrent.
    :param fast_resume_data: the bencoded fast resume data to save to
        disk
    """
    fast_resume_file = generate_fast_resume_filename(info_hash)
    fast_resume_dir = os.path.dirname(fast_resume_file)

    if not os.path.exists(fast_resume_dir):
        try:
            fileutil.makedirs(fast_resume_dir)
        except OSError:
            logging.exception("can't save fast_resume_data")
            return

    try:
        with open(fast_resume_file, 'wb') as f:
            f.write(fast_resume_data)
    except (OSError, IOError):
        logging.exception("Error occured trying to write fast_resume_data")
        try:
            os.unlink(fast_resume_file)
        except (OSError, IOError):
            pass

def load_fast_resume_data(info_hash):
    """Loads fast_resume_data from file on disk.

    :param info_hash: the torrent handle info hash--this is unique to
        a torrent.

    :returns: None if there are errors or it doesn't exist, or
        the bencoded fast resume data
    """
    fast_resume_file = generate_fast_resume_filename(info_hash)
    if not os.path.exists(fast_resume_file):
        return None

    try:
        f = open(fast_resume_file, "rb")
        fast_resume_data = f.read()
        f.close()
        return fast_resume_data
    except StandardError:
        logging.exception("exception kicked up when loading fast "
                          "resume data")
    return None

def remove_fast_resume_data(info_hash):
    """Removes fast_resume_data from file on disk.

    :param info_hash: the torrent handle info hash--this is unique to
        a torrent.
    """
    fast_resume_file = generate_fast_resume_filename(info_hash)
    if os.path.exists(fast_resume_file):
        try:
            fileutil.remove(fast_resume_file)
        except OSError:
            logging.exception("remove_fast_resume_data kicked up exception")

# update fast resume data every 5 seconds
FRD_UPDATE_LIMIT = 5

class BTDownloader(BGDownloader):
    # reannounce at most every 30 seconds
    REANNOUNCE_LIMIT = 30
    FRD_PROBLEMS = 0

    def __init__(self, url=None, item=None, restore=None, magnet=None):
        self.metainfo = None
        self.torrent = None
        self.rate = self.eta = 0
        self.upRate = self.uploaded = 0
        self.activity = None
        self.fast_resume_data = None
        self.retryDC = None
        self.channelName = None
        self.uploadedStart = 0
        self.restarting = False
        self.seeders = -1
        self.leechers = -1
        self.connections = -1
        self.metainfo_updated = False
        self.info_hash = None
        self.magnet = magnet
        self.get_delayed_metainfo = False
        if restore is not None:
            self.firstTime = False
            self.restore_state(restore)
        else:
            self.firstTime = True
            BGDownloader.__init__(self, url, item)
            self.run_downloader()

        self.item = item
        self._last_reannounce_time = time.time()
        self._last_frd_update = time.time()

    def _start_torrent(self):
        try:
            params = {}
            if self.magnet:
                duplicate = TORRENT_SESSION.find_duplicate_torrent_from_magnet(
                    self.magnet)
            else:
                torrent_info = lt.torrent_info(lt.bdecode(self.metainfo))
                params["ti"] = torrent_info
                self.totalSize = torrent_info.total_size()
                duplicate = TORRENT_SESSION.find_duplicate_torrent(
                    params["ti"])

            if duplicate is not None:
                c = command.DuplicateTorrent(daemon.LAST_DAEMON,
                        duplicate.dlid, self.dlid)
                c.send()
                return

            if self.firstTime and not self.accept_download_size(
                self.totalSize):
                self.handle_error(
                    _("Not enough disk space"),
                    _("%(amount)s MB required to store this video",
                      {"amount": self.totalSize / (2 ** 20)})
                    )
                return

            # the save_path needs to be a directory--that's where
            # libtorrent is going to save the torrent contents.
            params["save_path"] = self.calc_save_path()

            params["auto_managed"] = False
            params["paused"] = False
            params["duplicate_is_error"] = True

            # About file allocation: the default scheme that would work is
            # using sparse file allocation.  With sparse, physical disk space
            # is consumed on an as-needed basis and we no shuffling of the
            # individual file chunks is required (missing ones are zero-filled
            # on the fly and don't take up disk space).  Unfortunately not
            # all filesystems support sparse files and on those that don't
            # the results are less than ideal.  In particular, pause
            # immediately after a large file is started can take a long time
            # because the close() of the torrent file handle must wait till
            # all bytes are zero-filled.  Using compact mode has the
            # disadvantage of extra disk i/o because of pieces are moved
            # on the fly rather than being placed into its logical location
            # within the file but makes everything else usable again.
            params["storage_mode"] = lt.storage_mode_t.storage_mode_compact

            if self.info_hash:
                self.fast_resume_data = load_fast_resume_data(self.info_hash)
                if self.fast_resume_data:
                    params["resume_data"] = lt.bencode(self.fast_resume_data)

            if self.magnet:
                self.torrent = lt.add_magnet_uri(TORRENT_SESSION.session,
                                                 self.magnet.encode('utf-8'),
                                                 params)
            else:
                self.torrent = TORRENT_SESSION.session.add_torrent(params)

            if not self.firstTime:
                self.torrent.resume()

            self.info_hash = str(self.torrent.info_hash())

            # need to do this for libtorrent > 0.13
            self.torrent.auto_managed(False)
        except StandardError:
            self.handle_error(_('BitTorrent failure'),
                              _('BitTorrent failed to startup'))
            logging.exception("Exception thrown in _start_torrent")
        else:
            TORRENT_SESSION.add_torrent(self)

    def calc_save_path(self):
        """Get save_path to pass to libtorrent."""

        # The save_path is the directory of our filename, that's where
        # libtorrent is going to save the torrent contents.
        save_path = fileutil.expand_filename(os.path.dirname(self.filename))

        # FIXME - this is a check to make sure we're not encoding
        # something already encoded.  but what should really happen
        # is someone track down the life-cycle of save_path and
        # make sure it's correct.
        # bug 17120
        if isinstance(save_path, unicode):
            save_path = save_path.encode('utf-8')
        return save_path

    def scrape_tracker(self):
        logging.debug("%s: no metainfo--rescraping", self.item)

        # if we have no metainfo, then try rescraping
        try:
            self.torrent.scrape_tracker()
        except StandardError:
            # FIXME - lock this exception down
            logging.exception("unable to scrape tracker")

    def reannounce_to_peers(self):
        """Reannounce this peer to all the other peers.

        This method ensures we don't try to reannounce too often.
        """
        time_now = time.time()
        if time_now < (self._last_reannounce_time + self.REANNOUNCE_LIMIT):
            # reannounce every REANNOUNCE_LIMIT seconds at most
            return

        logging.debug("%s: reannouncing to peers", self.item)
        self._last_reannounce_time = time_now
        if not self.metainfo or len(self.torrent.trackers()) == 0:
            return

        if self.rate <= 0:
            # if the rate is 0, then try reannouncing
            try:
                self.torrent.force_reannounce()
            except StandardError:
                # FIXME - lock this exception down
                logging.exception("unable to reannounce to peers")

    def _shutdown_torrent(self):
        try:
            TORRENT_SESSION.remove_torrent(self)
            if self.torrent is not None:
                self.torrent.pause()
                self.update_fast_resume_data(force=True)
                TORRENT_SESSION.session.remove_torrent(self.torrent, 0)
                self.torrent = None
        except StandardError:
            logging.exception("Error shutting down torrent")

    def _pause_torrent(self):
        try:
            TORRENT_SESSION.remove_torrent(self)
            if self.torrent is not None:
                self.torrent.pause()
        except StandardError:
            logging.exception("Error pausing torrent")

    def _resume_torrent(self):
        if self.torrent is None:
            self._start_torrent()
            return

        try:
            self.torrent.resume()
            TORRENT_SESSION.add_torrent(self)
        except StandardError:
            logging.exception("Error resuming torrent")

    def _debug_print_peers(self):
        peers = self.torrent.get_peer_info()
        logging.debug("peers (%s):", self.item)
        for i, mem in enumerate(peers):
            if mem.flags & mem.connecting or mem.flags & mem.handshake:
                continue
            logging.debug("%4s: %12s down_speed %8s ip %20s progress %s",
                          i,
                          mem.client,
                          mem.down_speed,
                          mem.ip,
                          mem.progress)

    def _debug_print_status(self):
        logging.debug("update_status (%s): (activity: %s) (rate: %s) "
                      "(s: %s l: %s) (total_wanted_done: %s)",
                      self.item,
                      self.activity,
                      self.rate,
                      self.seeders,
                      self.leechers,
                      self.currentSize)

    def update_status(self):
        """
        activity -- string specifying what's currently happening or None for
                normal operations.
        upRate -- upload rate in B/s
        downRate -- download rate in B/s
        upTotal -- total MB uploaded
        downTotal -- total MB downloaded
        fractionDone -- what portion of the download is completed.
        timeEst -- estimated completion time, in seconds.
        totalSize -- total size of the torrent in bytes
        seeders -- number of seeders for this torrent
        leechers -- number of leechers for this torrent
        connecting -- nummber of peers we're connected to
        """
        status = self.torrent.status()
        self.totalSize = status.total_wanted
        self.rate = status.download_payload_rate
        self.upRate = status.upload_payload_rate
        self.uploaded = status.total_payload_upload + self.uploadedStart
        self.seeders = status.num_complete
        self.leechers = status.num_incomplete
        self.connections = status.num_connections
        try:
            self.eta = ((status.total_wanted - status.total_wanted_done) /
                        float(status.download_payload_rate))
        except ZeroDivisionError:
            self.eta = 0

        # FIXME - this needs some more examination before it's
        # enabled.
        # if self.rate == 0:
        #     self.reannounce_to_peers()

        if status.state == lt.torrent_status.states.queued_for_checking:
            self.activity = _("waiting to check existing files")
        elif status.state == lt.torrent_status.states.checking_files:
            self.activity = _("checking existing files")
        elif status.state == lt.torrent_status.states.allocating:
            self.activity = _("allocating disk space")
        else:
            self.activity = None

        self.currentSize = status.total_wanted_done

        # these are useful for debugging torrent issues
        # self._debug_print_status()
        # self._debug_print_peers()

        if ((self.state == u"downloading"
             and status.state == lt.torrent_status.states.seeding)):
            self.move_to_movies_directory()
            self.state = u"uploading"
            self.endTime = clock()

        self.update_client()

        if app.config.get(prefs.LIMIT_UPLOAD_RATIO):
            if status.state == lt.torrent_status.states.seeding:
                if ((float(self.uploaded) / self.totalSize >
                     app.config.get(prefs.UPLOAD_RATIO))):
                    self.stop_upload()

        # Initialize metadata once for magnet links
        if self.get_delayed_metainfo and self.torrent.has_metadata():
            self.got_delayed_metainfo()
            self.get_delayed_metainfo = False

        self.update_fast_resume_data()

    def update_fast_resume_data(self, force=False):
        if ((self.torrent is None or
             not self.torrent.has_metadata() or
             not self.info_hash)):
            return

        if BTDownloader.FRD_PROBLEMS >= 5:
            # if we've hit 5 problems, we don't keep trying
            return

        time_now = time.time()
        if ((not force and
             time_now < (self._last_frd_update + FRD_UPDATE_LIMIT))):
            return
        self._last_frd_update = time_now

        try:
            # FIXME - we should switch to save_resume_data which uses
            # an alert to save resume data rather than
            # write_resume_data which looks deprecated in 0.15.5.
            self.fast_resume_data = lt.bencode(
                self.torrent.write_resume_data())
        except RuntimeError, rte:
            # write_resume_data can kick up a
            # boost::filesystem::exists: Access is denied error.  If
            # that happens, we abort.  If it happens 5 times, we don't
            # bother trying again.  bug #16339.
            BTDownloader.FRD_PROBLEMS += 1
            logging.warning(
                "RuntimeError kicked up in update_fast_resume_data: %s", rte)
            return

        save_fast_resume_data(self.info_hash, self.fast_resume_data)

    def handle_error(self, short_reason, reason):
        self._shutdown_torrent()
        BGDownloader.handle_error(self, short_reason, reason)

    def handle_temporary_error(self, short_reason, reason):
        self._shutdown_torrent()
        BGDownloader.handle_temporary_error(self, short_reason, reason)

    def move_to_directory(self, directory):
        if self.state in (u'uploading', u'downloading'):
            self._shutdown_torrent()
            BGDownloader.move_to_directory(self, directory)
            self._resume_torrent()
        else:
            BGDownloader.move_to_directory(self, directory)

    def restore_state(self, data):
        # This is a bit of a hack since we currently don't
        # differentiate between magnet URIs and URLs anywhere else
        # than in this module
        if is_magnet_uri(data['url']):
            self.magnet = data['url']
            data['url'] = None
        self.__dict__.update(data)
        self.rate = self.eta = 0
        self.upRate = 0
        self.uploadedStart = self.uploaded
        if self.state in (u'downloading', u'uploading'):
            self.run_downloader(done=True)
        elif self.state == u'offline':
            self.start()

    def get_status(self):
        data = BGDownloader.get_status(self)
        data['upRate'] = self.upRate
        data['uploaded'] = self.uploaded
        if self.metainfo_updated:
            data['metainfo'] = self.metainfo
            self.metainfo_updated = False
        data['activity'] = self.activity
        data['dlerType'] = 'BitTorrent'
        data['seeders'] = self.seeders
        data['leechers'] = self.leechers
        data['connections'] = self.connections
        data['info_hash'] = self.info_hash
        return data

    def get_rate(self):
        return self.rate

    def get_eta(self):
        return self.eta

    def pause(self):
        self.state = u"paused"
        self.restarting = True
        self._pause_torrent()
        self.update_client()

    def stop(self, delete):
        self.state = u"stopped"
        self._shutdown_torrent()
        self.update_client()
        if delete:
            try:
                if fileutil.isdir(self.filename):
                    fileutil.rmtree(self.filename)
                else:
                    fileutil.remove(self.filename)
            except OSError:
                pass

            if self.info_hash:
                remove_fast_resume_data(self.info_hash)

    def stop_upload(self):
        self.state = u"finished"
        self._shutdown_torrent()
        self.update_client()

    def pause_upload(self):
        self.state = u"uploading-paused"
        self._shutdown_torrent()
        self.update_client()

    def start(self, resume=True):
        # for BT downloads, resume doesn't mean anything, so we ignore
        # it.
        if self.state not in (u'paused', u'stopped', u'offline'):
            return

        self.state = u"downloading"
        if self.retryDC:
            self.retryDC.cancel()
            self.retryDC = None
        self.update_client()
        self.get_metainfo()

    def shutdown(self):
        self._shutdown_torrent()
        self.update_client()

    def got_metainfo(self):
        # FIXME: If the client is stopped before a BT download gets
        #        its metadata, we never run this. It's not a huge deal
        #        because it only affects the incomplete filename
        if not self.restarting:
            try:
                metainfo = lt.bdecode(self.metainfo)
                # if we don't get valid torrent metadata back, then
                # the metainfo is None.  treat that like a runtime
                # error.
                if not metainfo:
                    raise RuntimeError()
                name = metainfo['info']['name']
                # if the metainfo['info'] has a files key, then this
                # is a torrent of a bunch of files in a directory
                is_directory = "files" in metainfo['info']

            # Note: handle KeyError as well because bdecode() may
            # return an object with no 'info' key, or with 'info' key
            # but no 'name' key.  This allows us to catch lousily made
            # torrent files.
            except (KeyError, RuntimeError):
                self.handle_corrupt_torrent()
                return
            self.shortFilename = utf8_to_filename(name)
            try:
                self.pick_initial_filename(
                    suffix="", torrent=True, is_directory=is_directory)

            # Somewhere deep it calls makedirs() which can throw
            # exceptions.
            #
            # Not sure if this is correct but if we throw a runtime
            # error like above it can't hurt anyone.  ValueError to catch
            # next_free_filename().
            except (ValueError, OSError, IOError):
                raise RuntimeError
        self.update_client()
        self._resume_torrent()

    def got_delayed_metainfo(self):
        """This does the same as got_metainfo, but for magnet links.
        While got_metainfo is called before a torrent is added
        got_delayed_metainfo() has to be called after a torrent is
        added and it has received the metainfo for the magnet link.
        """
        if not self.torrent.has_metadata():
            return
        self.shortFilename =  utf8_to_filename(
            self.torrent.get_torrent_info().name())

        # FIXME: we should determine whether it is a directory
        # in the same way in got_metainfo and got_delayed_metainfo
        is_directory = False
        for file_ in self.torrent.get_torrent_info().files():
            if os.sep in file_.path:
                is_directory = True

        is_directory = (is_directory or 
                       len(self.torrent.get_torrent_info().files()) > 1)
        try:
            self.pick_initial_filename(
                suffix="", torrent=True, is_directory=is_directory)
            # Somewhere deep it calls makedirs() which can throw
            # exceptions.
            #
            # Not sure if this is correct but if we throw a runtime
            # error like above it can't hurt anyone.
        except (OSError, IOError):
            raise RuntimeError
        save_path = self.calc_save_path()
        self.torrent.move_storage(save_path)


    def handle_corrupt_torrent(self):
        self.handle_error(
            _("Corrupt Torrent"),
            _("The torrent file at %(url)s was not valid",
              {"url": stringify(self.url)})
            )

    def handle_metainfo(self, metainfo):
        self.metainfo = metainfo
        self.metainfo_updated = True
        self.got_metainfo()

    def check_description(self, data):
        if len(data) > MAX_TORRENT_SIZE or data[0] != 'd':
            # Bailout if we get too much data or it doesn't begin with
            # "d" (see #12301 for details)
            eventloop.add_idle(self.handle_corrupt_torrent,
                               'description check failed')
            return False
        else:
            return True

    def on_metainfo_download(self, info):
        self.handle_metainfo(info['body'])

    def on_metainfo_download_error(self, exception):
        self.handle_network_error(exception)

    def get_metainfo(self):
        # If it's a magnet link, skip getting meta info
        if self.magnet:
            # Use this to signal that the metainfo is not yet
            # available and should be added once they are available
            # from the torrent.
            self.get_delayed_metainfo = True
            # This skips got metainfo and calls update_client() and
            # _resume_torrent() directly.
            self.update_client()
            self._resume_torrent()
            return
        elif self.metainfo is None:
            if self.url.startswith('file://'):
                path = get_file_url_path(self.url)
                try:
                    metainfo_file = open(path, 'rb')
                except IOError:
                    self.handle_error(
                        _("Torrent file deleted"),
                        _("The torrent file for this item was deleted "
                          "outside of %(appname)s.",
                          {"appname": app.config.get(prefs.SHORT_APP_NAME)}
                          ))

                    return
                try:
                    metainfo = metainfo_file.read()
                finally:
                    metainfo_file.close()

                self.handle_metainfo(metainfo)
            else:
                self.description_client = httpclient.grab_url(self.url,
                        self.on_metainfo_download,
                        self.on_metainfo_download_error,
                        content_check_callback=self.check_description)
        else:
            self.got_metainfo()

    def run_downloader(self, done=False):
        self.restarting = done
        self.update_client()
        self.get_metainfo()
