# Miro - an RSS based video player application
# Copyright (C) 2009, 2010, 2011
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

"""``miro.commandline`` -- This modules handles the parsing of
files/URLs passed to Miro on the command line.

Frontends should call ``set_command_line_args()`` passing it a list of
arguments that the users gives.  This should just be suspected
torrents/videos, not things like ``--help``, ``--version``, etc.

Frontends should trap when a user opens a torrent/video with Miro
while Miro is already running.  They should arrange for ``add_video``
or ``add_torrent`` to be called in the existing Miro process.
"""

from miro.gtcache import gettext as _
import time

import os.path
import logging
from miro import app
from miro import eventloop
from miro import prefs
from miro import messages
from miro import dialogs
from miro import autodiscover
from miro import subscription
from miro import feed
from miro import fileutil
from miro import item
from miro import itemsource
from miro import httpclient
from miro import download_utils
from miro.util import get_torrent_info_hash, is_magnet_uri
from miro.plat.utils import samefile, filename_to_unicode
from miro import singleclick
from miro import opml

_command_line_args = []
_started_up = False
_command_line_videos = None
_command_line_view = None

def add_video(path, manual_feed=None):
    """Add a new video

    :returns: True if we create a new Item object.
    """
    path = os.path.abspath(path)
    if item.Item.have_item_for_path(path):
        logging.debug("Not adding duplicate video: %s",
                      path.decode('ascii', 'ignore'))
        # get the first item and undelete it
        item_for_path = list(item.Item.items_with_path_view(path))[0]
        if item_for_path.deleted:
            item_for_path.make_undeleted()
        if _command_line_videos is not None:
            _command_line_videos.add(item_for_path)
        return False
    if manual_feed is None:
        manual_feed = feed.Feed.get_manual_feed()
    file_item = item.FileItem(
        path, feed_id=manual_feed.get_id(), mark_seen=True)
    if _command_line_videos is not None and file_item.id_exists():
        _command_line_videos.add(file_item)
    return True

@eventloop.idle_iterator
def add_videos(paths):
    # filter out non-existent paths
    paths = [p for p in paths if fileutil.exists(p)]
    path_iter = iter(paths)
    finished = False
    yield # yield after doing prep work
    with app.local_metadata_manager.bulk_add():
        while not finished:
            finished = _add_batch_of_videos(path_iter, 0.1)
            yield # yield after each batch

def _add_batch_of_videos(path_iter, max_time):
    """Add a batch of videos for add_video()

    This method consumes the paths in path_iter until the iterator finishes,
    or max_time elapses.  It creates videos with add_video().

    :returns: True if we returned because path_iter was finished
    """
    start_time = time.time()
    manual_feed = feed.Feed.get_manual_feed()
    app.bulk_sql_manager.start()
    try:
        for path in path_iter:
            add_video(path, manual_feed=manual_feed)
            if time.time() - start_time > max_time:
                return False
        return True
    finally:
        app.bulk_sql_manager.finish()

def add_torrent(path, torrent_info_hash):
    manual_feed = feed.Feed.get_manual_feed()
    for i in manual_feed.items:
        if ((i.downloader is not None
             and i.downloader.status.get('infohash') == torrent_info_hash)):
            logging.info("not downloading %s, it's already a download for %s",
                         path, i)
            if i.downloader.get_state() in ('paused', 'stopped'):
                i.download()
            return
    new_item = item.Item(item.fp_values_for_file(path),
                         feed_id=manual_feed.get_id())
    new_item.download()

def _complain_about_subscription_url(message_text):
    title = _("Subscription error")
    dialogs.MessageBoxDialog(title, message_text).run()

def add_subscription_url(prefix, expected_content_type, url):
    real_url = url[len(prefix):]
    def callback(info):
        if info.get('content-type') == expected_content_type:
            subscription_list = autodiscover.parse_content(info['body'])
            if subscription_list is None:
                text = _(
                    "This %(appname)s podcast file has an invalid format: "
                    "%(url)s.  Please notify the publisher of this file.",
                    {"appname": app.config.get(prefs.SHORT_APP_NAME),
                     "url": real_url}
                    )
                _complain_about_subscription_url(text)
            else:
                subscription.Subscriber().add_subscriptions(
                    subscription_list)
        else:
            text = _(
                "This %(appname)s podcast file has the wrong content type: "
                "%(url)s. Please notify the publisher of this file.",
                {"appname": app.config.get(prefs.SHORT_APP_NAME),
                 "url": real_url}
                )
            _complain_about_subscription_url(text)

    def errback(error):
        text = _(
            "Could not download the %(appname)s podcast file: %(url)s",
            {"appname": app.config.get(prefs.SHORT_APP_NAME),
             "url": real_url}
            )
        _complain_about_subscription_url(text)

    httpclient.grab_url(real_url, callback, errback)

def set_command_line_args(args):
    _command_line_args.extend(args)

def reset_command_line_view():
    global _command_line_view, _command_line_videos
    if _command_line_view is not None:
        _command_line_view.unlink()
        _command_line_view = None
    _command_line_videos = set()

def parse_command_line_args(args):
    """
    This goes through a list of files which could be arguments passed
    in on the command line or a list of files from other source.
    """
    if not _started_up:
        _command_line_args.extend(args)
        return

    for i in xrange(len(args)):
        if args[i].startswith('file://'):
            args[i] = args[i][len('file://'):]

    reset_command_line_view()

    added_videos = False
    added_downloads = False

    for arg in args:
        if arg.startswith('file://'):
            arg = download_utils.get_file_url_path(arg)
        elif arg.startswith('miro:'):
            add_subscription_url('miro:', 'application/x-miro', arg)
        elif arg.startswith('democracy:'):
            add_subscription_url('democracy:', 'application/x-democracy', arg)
        elif (arg.startswith('http:')
              or arg.startswith('https:')
              or arg.startswith('feed:')
              or arg.startswith('feeds:')
              or is_magnet_uri(arg)):
            singleclick.add_download(filename_to_unicode(arg))
        elif os.path.exists(arg):
            ext = os.path.splitext(arg)[1].lower()
            if ext in ('.torrent', '.tor'):
                try:
                    torrent_infohash = get_torrent_info_hash(arg)
                except ValueError:
                    title = _("Invalid Torrent")
                    msg = _(
                        "The torrent file %(filename)s appears to be corrupt "
                        "and cannot be opened.",
                        {"filename": os.path.basename(arg)}
                        )
                    dialogs.MessageBoxDialog(title, msg).run()
                    continue
                except (IOError, OSError):
                    title = _("File Error")
                    msg = _(
                        "The torrent file %(filename)s could not be opened. "
                        "Please ensure it exists and you have permission to "
                        "access this file.",
                        {"filename": os.path.basename(arg)}
                        )
                    dialogs.MessageBoxDialog(title, msg).run()
                    continue
                add_torrent(arg, torrent_infohash)
                added_downloads = True
            elif ext in ('.rss', '.rdf', '.atom', '.ato'):
                feed.add_feed_from_file(arg)
            elif ext in ('.miro', '.democracy', '.dem', '.opml'):
                opml.Importer().import_subscriptions(arg, show_summary=False)
            else:
                add_video(arg)
                added_videos = True
        else:
            logging.warning("parse_command_line_args: %s doesn't exist", arg)

    # if the user has Miro set up to play all videos externally, then
    # we don't want to play videos added by the command line.
    #
    # this fixes bug 12362 where if the user has his/her system set up
    # to use Miro to play videos and Miro goes to play a video
    # externally, then it causes an infinite loop and dies.
    if added_videos and app.config.get(prefs.PLAY_IN_MIRO):
        item_infos = [itemsource.DatabaseItemSource._item_info_for(i)
                      for i in _command_line_videos]
        messages.PlayMovie(item_infos).send_to_frontend()

    if added_downloads:
        # FIXME - switch to downloads tab?
        pass

def startup():
    global _command_line_args
    global _started_up
    _started_up = True
    parse_command_line_args(_command_line_args)
    _command_line_args = []
