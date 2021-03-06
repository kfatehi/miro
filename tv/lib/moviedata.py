# Miro - an RSS based video player application
# Copyright (C) 2006, 2006, 2007, 2008, 2009, 2010, 2011
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

from miro.eventloop import as_idle
import os.path
import re
import subprocess
import tempfile
import time
import traceback
import threading
import Queue
import logging
from contextlib import contextmanager

from miro import app
from miro import prefs
from miro import signals
from miro import util
from miro import fileutil
from miro import workerprocess

# Time in seconds that we wait for the utility to execute.  If it goes
# longer than this, we assume it's hung and kill it.
MOVIE_DATA_UTIL_TIMEOUT = 30

class State(object):
    """Enum for tracking what we've looked at.

    None indicates that we haven't looked at the file at all;
    non-true values indicate that we haven't run MDP.
    """
    UNSEEN = None
    SKIPPED = 0
    RAN = 1
    FAILED = 2

class MovieDataInfo(object):
    """Little utility class to keep track of data associated with each
    movie.  This is:

    * The item.
    * The path to the video.
    * Path to the thumbnail we're trying to make.
    * List of commands that we're trying to run, and their environments.
    """
    def __init__(self, item):
        self.item = item
        self.video_path = item.get_filename()
        self.thumbnail_path = self._make_thumbnail_path()

    def _make_thumbnail_path(self):
        # add a random string to the filename to ensure it's unique.
        # Two videos can have the same basename if they're in
        # different directories.
        video_base = os.path.basename(self.video_path)
        filename = '%s.%s.png' % (video_base, util.random_string(5))
        return os.path.join(self.image_directory('extracted'), filename)

    @classmethod
    def image_directory(cls, subdir):
        dir_ = os.path.join(app.config.get(prefs.ICON_CACHE_DIRECTORY), subdir)
        try:
            fileutil.makedirs(dir_)
        except OSError:
            pass
        return dir_

class MovieDataUpdater(object):
    def __init__ (self):
        self.in_shutdown = False
        self.in_progress = set()

    def _path_processed(self, mdi):
        if hasattr(app, 'metadata_progress_updater'): # hack for unittests
            app.metadata_progress_updater.path_processed(mdi.video_path)

    def errback(self, result, mdi):
        logging.debug('moviedata: FAILED! result = %s', result)
        self.update_failed(mdi.item)
        self._path_processed(mdi)

    def callback(self, result, mdi):
        mediatype, duration, got_screenshot = result

        # Make sure this is unicode, or else database validation will
        # fail on insert!
        mediatype = unicode(mediatype)

        if os.path.splitext(mdi.video_path)[1] == '.flv':
            # bug #17266.  if the extension is .flv, we ignore the mediatype
            # we just got from the movie data program.  this is
            # specifically for .flv files which the movie data
            # extractors have a hard time with.
            mediatype = u'video'

        if fileutil.exists(mdi.thumbnail_path) and got_screenshot:
            screenshot = mdi.thumbnail_path
        else:
            screenshot = None

        # bz:17364/bz:18072 HACK: need to avoid UnicodeDecodeError -
        # until we do a proper pathname cleanup.  Used to be a %s with a
        # encode to utf-8 but then 18072 came up.  It seems that this
        # can either be a str OR a unicode.  I don't really feel
        # like dealing with this right now, so just use %r.
        logging.debug("moviedata: mdp %s %s %s %r", duration, screenshot,
                mediatype, mdi.video_path)

        # Repack the thing, as we may have changed it
        self.update_finished(mdi.item, duration, screenshot, mediatype)
        self._path_processed(mdi)

    def update_failed(self, item):
        self.in_progress.remove(item.id)
        if item.id_exists():
            item.mdp_state = State.FAILED
            if item.has_drm:
                #17442#c7, part2: if mutagen called it potentially DRM'd and we
                # couldn't read it, we consider it DRM'd; files that we consider
                # DRM'd initially go in "Other"
                item.file_type = u'other'
            item.signal_change()

    def update_finished(self, item, duration, screenshot, mediatype):
        self.in_progress.remove(item.id)
        if item.id_exists():
            item.mdp_state = State.RAN
            item.screenshot = screenshot
            if duration is not None:
                item.duration = duration
                if duration != -1:
                    # if mutagen thought it might have DRM but we got a
                    # duration, override mutagen's guess
                    item.has_drm = False
            if item.has_drm:
                #17442#c7, part2: if mutagen called it potentially DRM'd and we
                # couldn't read it, we consider it DRM'd; files that we consider
                # DRM'd initially go in "Other"
                item.file_type = u'other'
            elif mediatype is not None:
                item.file_type = mediatype
            item.signal_change()

    def update_skipped(self, item):
        item.mdp_state = State.SKIPPED
        item.signal_change()

    def request_update(self, item):
        if (hasattr(app, 'in_unit_tests') and
                not hasattr(app, 'testing_mdp')):
            # kludge for skipping MDP in non-MDP unittests
            return
        if self.in_shutdown:
            return
        if item.id in self.in_progress:
            return

        if self._should_process_item(item):
            self.in_progress.add(item.id)
            info = MovieDataInfo(item)
            workerprocess.run_media_metadata_extractor(
              info.video_path,
              info.thumbnail_path,
              lambda result: self.callback(result, info),
              lambda result: self.errback(result, info))
        else:
            self.update_skipped(item)
            app.metadata_progress_updater.path_processed(item.get_filename())

    def _should_process_item(self, item):
        if item.has_drm:
            # mutagen can only identify files that *might* have drm, so we
            # always need to check that
            return True
        # Only run the movie data program for video items, audio items that we
        # don't know the duration for, or items that do not have "other"
        # filenames that mutagen could not determine type for.
        return (item.file_type == u'video' or
                (item.file_type == u'audio' and item.duration is None) or
                item.file_type is None)

    def shutdown(self):
        self.in_shutdown = True
