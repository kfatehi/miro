# Miro - an RSS based video player application
# Copyright (C) 2010 Participatory Culture Foundation
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

import errno
import os
import socket
import select
import struct
import threading
import time

from miro.gtcache import gettext as _
from miro import app
from miro import config
from miro import eventloop
from miro import item
from miro import messages
from miro import playlist
from miro import prefs
from miro import util
from miro.fileobject import FilenameType
from miro.util import returns_filename

from miro.plat.utils import thread_body

import libdaap

# Windows Python does not have inet_ntop().  Sigh.  Fallback to this one,
# which isn't as good, if we do not have access to it.
def inet_ntop(af, ip):
    try:
        return socket.inet_ntop(af, ip)
    except AttributeError:
        if af == socket.AF_INET:
            return socket.inet_ntoa(ip)
        if af == soket.AF_INET6:
            return ':'.join('%x' % bit for bit in struct.unpack('!' + 'H' * 8,
                                                                ip))
        raise ValueError('unknown address family %d' % af)

# Helper utilities
# Translate neutral constants to native protocol constants with this, or
# fixup strings if necessary.
def daap_item_fixup(item_id, entry):
    daapitem = []

    # Easy ones - can do a direct translation
    mapping = [('name', 'minm'), ('enclosure_format', 'asfm'),
               ('size', 'assz')]
    for p, q in mapping:
        if isinstance(entry[p], unicode):
            attribute = (q, entry[p].encode('utf-8'))
        else:
            attribute = (q, entry[p])
        daapitem.append(attribute)

    # Manual ones

    # Tack on the ID.
    daapitem.append(('miid', item_id))
    # Convert the duration to milliseconds, as expected.
    daapitem.append(('astm', entry['duration'] * 1000))
    # Also has movie or tv shows but Miro doesn't support it so make it
    # a generic video.
    if entry['file_type'] == 'video':
        daapitem.append(('aeMK', libdaap.DAAP_MEDIAKIND_VIDEO))
    else:
        daapitem.append(('aeMK', libdaap.DAAP_MEDIAKIND_AUDIO))

    return daapitem

class SharingItem(object):
    """
    An item which lives on a remote share.
    """
    def __init__(self, **kwargs):
        for required in ('video_path', 'id', 'file_type', 'host', 'port'):
            if required not in kwargs:
                raise TypeError('SharingItem must be given a "%s" argument'
                                % required)
        self.name = self.file_format = self.size = None
        self.release_date = self.feed_name = self.feed_id = None
        self.keep = self.media_type_checked = True
        self.updating_movie_info = self.isContainerItem = False
        self.url = self.payment_link = None
        self.comments_link = self.permalink = self.file_url = None
        self.license = self.downloader = None
        self.duration = self.screenshot = self.thumbnail_url = None
        self.resumeTime = 0
        self.subtitle_encoding = self.enclosure_type = None
        self.description = u''
        self.metadata = {}
        self.rating = None
        self.file_type = None
        self.creation_time = None

        self.__dict__.update(kwargs)

        self.video_path = FilenameType(self.video_path)
        if self.name is None:
            self.name = _("Unknown")
        # Do we care about file_format?
        if self.file_format is None:
            pass
        if self.size is None:
            self.size = 0
        if self.release_date is None or self.creation_time is None:
            now = time.time()
            if self.release_date is None:
                self.release_date = now
            if self.creation_time is None:
                self.creation_time = now
        if self.duration is None: # -1 is unknown
            self.duration = 0

    @staticmethod
    def id_exists():
        return True

    @returns_filename
    def get_filename(self):
        # For daap, sent it to be the same as http as it is basically
        # http with a different port.
        def daap_handler(path, host, port):
            return 'http://%s:%s%s' % (host, port, path)
        fn = FilenameType(self.video_path)
        fn.set_handler(daap_handler, [self.host, self.port])
        return fn

    def get_url(self):
        return self.url or u''

    @returns_filename
    def get_thumbnail(self):
        # What about cover art?
        if self.file_type == 'audio':
            return resources.path("images/thumb-default-audio.png")
        else:
            return resources.path("images/thumb-default-video.png")

    def _migrate_thumbnail(self):
        # This should not ever do anything useful.  We don't have a backing
        # database to safe this stuff.
        pass

    def remove(self, save=True):
        # This should never do anything useful, we don't have a backing
        # database. Yet.
        pass

class SharingTracker(object):
    """The sharing tracker is responsible for listening for available music
    shares and the main client connection code.  For each connected share,
    there is a separate SharingItemTrackerImpl() instance which is basically
    a backend for messagehandler.SharingItemTracker().
    """
    type = u'sharing'
    def __init__(self):
        self.trackers = dict()
        self.available_shares = []
        self.r, self.w = util.make_dummy_socket_pair()

    def calc_local_addresses(self):
        # Get our own hostname so that we can filter out ourselves if we 
        # also happen to be broadcasting.  Getaddrinfo() may block so you 
        # MUST call in auxiliary thread context.
        #
        # Why isn't this cached, you may ask?  Because the system may
        # change IP addresses while this program is running then we'd be
        # filtering the wrong addresses.  
        #
        # XXX can I count on the Bonjour daemon implementation to send me
        # the add/remove messages when the IP changes?
        hostname = socket.gethostname()
        local_addresses = []
        try:
            addrinfo = socket.getaddrinfo(hostname, 0, 0, 0, socket.SOL_TCP)
            for family, socktype, proto, canonname, sockaddr in addrinfo:
                local_addresses.append(canonname)
        except socket.error, (err, errstring):
            # What am I supposed to do here?
            pass

        return local_addresses

    def mdns_callback(self, added, fullname, host, ips, port):
        eventloop.add_urgent_call(self.mdns_callback_backend, "mdns callback",
                                  args=[added, fullname, host, ips, port])

    def mdns_callback_backend(self, added, fullname, host, ips, port):
        added_list = []
        removed_list = []
        unused, local_port = app.sharing_manager.get_address()
        local_addresses = self.calc_local_addresses()
        ip_values = [inet_ntop(k, ips[k]) for k in ips.keys()]
        if set(local_addresses + ip_values) and local_port == port:
            return
        # Need to come up with a unique ID for the share.  Use 
        # (name, host, port)
        share_id = (fullname, host, port)
        # Do we have this share on record?  If so then just ignore.
        # In particular work around a problem with Avahi apparently sending
        # duplicate messages.
        if added and share_id in self.available_shares:
            return
        if not added and not share_id in self.available_shares:
            return 

        info = messages.SharingInfo(share_id, fullname, host, port)
        if added:
            added_list.append(info)
            self.available_shares.append(share_id)
        else:
            removed_list.append(share_id)
            self.available_shares.append(share_id)
            # XXX we should not be simply stopping because the mDNS share
            # disappears.  AND we should not be calling this from backend
            # due to RACE!
            item = app.playback_manager.get_playing_item()
            remote_item = False
            if item and item.remote:
                remote_item = True
            if remote_item and item.host == host and item.port == port:
                app.playback_manager.stop(save_resume_time=False)
        # XXX should not remove this tab if it is currently mounted.  The
        # mDNS going away just means it is no longer published, doesn't
        # mean it's not available.
        message = messages.TabsChanged('sharing', added_list, [], removed_list) 
        message.send_to_frontend()

    def server_thread(self):
        callback = libdaap.browse_mdns(self.mdns_callback)
        while True:
            refs = callback.get_refs()
            try:
                r, w, x = select.select(refs + [self.r], [], [])
                for i in r:
                    if i in refs:
                        callback(i)
                        continue
                    if i == self.r:
                        return
            # XXX what to do in case of error?  How to pass back to user?
            except select.error, (err, errstring):
                if err == errno.EINTR:
                    continue
                else:
                    pass
            except:
                pass

    def start_tracking(self):
        # sigh.  New thread.  Unfortunately it's kind of hard to integrate
        # it into the application runloop at this moment ...
        self.thread = threading.Thread(target=thread_body,
                                       args=[self.server_thread],
                                       name='mDNS Browser Thread')
        self.thread.start()

    def eject(self, share_id):
        tracker = self.trackers[share_id]
        del self.trackers[share_id]
        tracker.disconnect()

    def get_tracker(self, tab, share_id):
        try:
            return self.trackers[share_id]
        except KeyError:
            print 'CREATING NEW TRACKER'
            self.trackers[share_id] = SharingItemTrackerImpl(tab, share_id)
            return self.trackers[share_id]

    def stop_tracking(self):
        # What to do in case of socket error here?
        self.w.send("b")

# Synchronization issues: The messagehandler.SharingItemTracker() creates
# one of these for each share it connects to.  If this is an initial connection
# the send_initial_list() will be empty and it will send the actual list
# after connected, which must happen strictly after send_initial_list() as
# both are scheduled to run on the backend thread.  If this is not an initial
# connection then send_initial_list() would already have been populated so
# we are fine there.
class SharingItemTrackerImpl(object):
    """This is the backend for the SharingItemTracker the messagehandler file.
    This backend class allows the item tracker to be persistent even as the
    user switches across different tabs in the sidebar, until the disconnect
    button is clicked.
    """
    type = u'sharing'
    def __init__(self, tab, share_id):
        self.tab = tab
        self.id = share_id
        self.items = []
        eventloop.call_in_thread(self.client_connect_callback,
                                 self.client_connect_error_callback,
                                 self.client_connect,
                                 'DAAP client connect')

    def sharing_item(self, rawitem):
        file_type = u'audio'    # fallback
        if rawitem['file_type'] == libdaap.DAAP_MEDIAKIND_AUDIO:
            file_type = u'audio'
        if rawitem['file_type'] in [libdaap.DAAP_MEDIAKIND_TV,
                                    libdaap.DAAP_MEDIAKIND_MOVIE,
                                    libdaap.DAAP_MEDIAKIND_VIDEO
                                   ]:
            file_type = u'video'
        sharing_item = SharingItem(
            id=rawitem['id'],
            duration=rawitem['duration'],
            size=rawitem['size'],
            name=rawitem['name'].decode('utf-8'),
            file_type=file_type,
            host=self.client.host,
            port=self.client.port,
            video_path=self.client.daap_get_file_request(rawitem['id'])
        )
        return sharing_item

    def disconnect(self):
        ids = [item.id for item in self.get_items()]
        message = messages.ItemsChanged(self.type, self.tab, [], [], ids)
        self.items = []
        print 'SENDING removed message'
        message.send_to_frontend()
        # No need to clean out our list of items as we are going away anyway.
        # As close() can block, run in separate thread.
        eventloop.call_in_thread(self.client_disconnect_callback,
                                 self.client_disconnect_error_callback,
                                 lambda: self.client.disconnect(),
                                 'DAAP client disconnect')

    def client_disconnect_error_callback(self, unused):
        pass

    def client_disconnect_callback(self, unused):
        pass

    def client_connect(self):
        print 'client_thread: running'
        # The id actually encodes (name, host, port).
        name, host, port = self.id
        self.client = libdaap.make_daap_client(host, port)
        if not self.client.connect():
            # XXX API does not allow us to send more detailed results
            # back to the poor user.
            raise IOError('Cannot connect')
        # XXX no API for this?  And what about playlists?
        # XXX dodgy - shouldn't do this directly
        # Find the base playlist, then suck all data out of it and then
        # return as a ItemsChanged message
        for k in self.client.playlists.keys():
            if self.client.playlists[k]['base']:
                break
        # Maybe we have looped through here without a base playlist.  Then
        # the server is broken.
        if not self.client.playlists[k]['base']:
            print 'no base list?'
            return
        items = self.client.items[k]
        print 'XXX CREATE ITEM INFO'
        #for k in items.keys():
        #    item = messages.ItemInfo(self.sharing_item(items[k]))
        #    self.items.append(item)

    # NB: this runs in the eventloop (backend) thread.
    def client_connect_callback(self, unused):
        self.connected = True
        message = messages.ItemsChanged(self.type, self.tab, self.items, [], [])
        print 'SENDING changed message %d items' % len(message.added)
        message.send_to_frontend()

    def client_connect_error_callback(self, unused):
        # If it didn't work, immediately disconnect ourselves.
        app.sharing_tracker.eject(self.id)
        messages.SharingConnectFailed(self.tab, self.id).send_to_frontend()

    def get_items(self):
        return self.items

class SharingManagerBackend(object):
    """SharingManagerBackend is the bridge between pydaap and Miro.  It
    pushes Miro media items to pydaap so pydaap can serve them to the outside
    world."""
    types = ['videos', 'music']
    id    = None                # Must be None
    items = dict()              # Neutral format - not really needed.
    daapitems = dict()          # DAAP format XXX - index via the items
    # XXX daapplaylist should be hidden from view. 
    daap_playlists = dict()     # Playlist, in daap format
    playlist_item_map = dict()  # Playlist -> item mapping

    # Reserved for future use: you can register new sharing protocols here.
    def register_protos(self, proto):
        pass

    def handle_item_list(self, message):
        self.make_item_dict(message.items)

    def handle_items_changed(self, message):
        # If items are changed, just redelete and recreate the entry.
        for itemid in message.removed:
            del self.items[itemid]
            del self.daapitems[itemid]
        self.make_item_dict(message.added)
        self.make_item_dict(message.changed)

    # Note: this should really be a util function and be separated
    def make_daap_playlists(self, items):
        # Pants.  We reuse the function but the view from the database is
        # different to the message the frontend sends to us!  So, minm is
        # duplicated.
        mappings = [('name', 'minm'), ('title', 'minm'), ('id', 'miid'),
                    ('id', 'mper')]
        for x in items:
            attributes = []
            for p, q in mappings:
                try:
                    if isinstance(getattr(x, p), unicode):
                        attributes.append((q, getattr(x, p).encode('utf-8')))
                    else:
                        attributes.append((q, getattr(x, p)))
                except AttributeError:
                    # Didn't work.  Oh well, get the next one.
                    continue
            # At this point, the item list has not been fully populated yet.
            # Therefore, it may not be possible to run get_items() and getting
            # the count attribute.  Instead we use the playlist_item_map.
            tmp = [y for y in playlist.PlaylistItemMap.playlist_view(x.id)]
            count = len(tmp)
            attributes.append(('mpco', 0))        # Parent container ID
            attributes.append(('mimc', count))    # Item count
            self.daap_playlists[x.id] = attributes

    def handle_playlist_added(self, obj, added):
        self.make_daap_playlists(added)

    def handle_playlist_changed(self, obj, changed):
        for x in changed:
            del self.daap_playlists[x.id]
        self.make_daap_playlists(changed)

    def handle_playlist_removed(self, obj, removed):
        for x in removed:
            del self.daap_playlists[x]

    def populate_playlists(self):
        self.make_daap_playlists(playlist.SavedPlaylist.make_view())
        for playlist_id in self.daap_playlists.keys():
            self.playlist_item_map[playlist_id] = [x.item_id
              for x in playlist.PlaylistItemMap.playlist_view(playlist_id)]

    def start_tracking(self):
        for t in self.types:
            app.info_updater.item_list_callbacks.add(t, self.id,
                                                self.handle_item_list)
            app.info_updater.item_changed_callbacks.add(t, self.id,
                                                self.handle_items_changed)

        self.populate_playlists()

        app.info_updater.connect('playlists-added',
                                 self.handle_playlist_added)
        app.info_updater.connect('playlists-changed',
                                 self.handle_playlist_changed)
        app.info_updater.connect('playlists-removed',
                                 self.handle_playlist_removed)

    def stop_tracking(self):
        for t in self.types:
            app.info_updater.item_list_callbacks.remove(t, self.id,
                                                self.handle_item_list)
            app.info_updater.item_changed_callbacks.remove(t, self.id,
                                                self.handle_items_changed)

        app.info_updater.disconnect(self.handle_playlist_added)
        app.info_updater.disconnect(self.handle_playlist_changed)
        app.info_updater.disconnect(self.handle_playlist_removed)

    def get_filepath(self, itemid):
        return self.items[itemid]['path']

    def get_playlists(self):
        playlists = []
        for k in self.daap_playlists.keys():
            playlists.append(('mlit', self.daap_playlists[k]))
        return playlists

    def get_items(self, playlist_id=None):
        # FIXME Guard against handle_item_list not having been run yet?
        # But if it hasn't been run, it really means at there are no items
        # (at least, in the eyes of Miro at this stage).
        # XXX cache me.  Ideally we cache this per-protocol then we create
        # this eagerly, then the self.items becomes a mapping from proto
        # to a list of items.

        # Easy: just return
        if not playlist_id:
            return self.daapitems
        # XXX Somehow cache this?
        playlist = dict()
        for x in self.daapitems.keys():
            if x in self.playlist_item_map[playlist_id]:
                playlist[x] = self.daapitems[x]
        return playlist

    def make_item_dict(self, items):
        # See lib/messages.py for a list of full fields that can be obtained
        # from an ItemInfo.  Note that, this only contains partial information
        # as it does not contain metadata about the item.  We do make one or
        # two assumptions here, in particular the file_type will always either
        # be video or audio.  For the actual file extension we strip it off
        # from the actual file path.  We create a dict object for this,
        # which is not very economical.  Is it possible to just keep a 
        # reference to the ItemInfo object?
        interested_fields = ['id', 'name', 'size', 'file_type', 'file_format',
                             'video_path', 'duration']
        for x in items:
            name = x.name
            size = x.size
            duration = x.duration
            file_type = x.file_type
            path = x.video_path
            f, e = os.path.splitext(path)
            # Note! sometimes this doesn't work because the file has no
            # extension!
            if e:
                e = e[1:]
            self.items[x.id] = dict(name=name, size=size, duration=duration,
                                  file_type=file_type, path=path,
                                  enclosure_format=e)
            self.daapitems[x.id] = daap_item_fixup(x.id, self.items[x.id])

class SharingManager(object):
    """SharingManager is the sharing server.  It publishes Miro media items
    to the outside world.  One part is the server instance and the other
    part is the service publishing, both are handled here.
    """
    CMD_QUIT = 'quit'
    CMD_NOP  = 'nop'
    def __init__(self):
        self.r, self.w = util.make_dummy_socket_pair()
        self.sharing = False
        self.discoverable = False
        self.config_watcher = config.ConfigWatcher(
            lambda func, *args: eventloop.add_idle(func, 'config watcher',
                 args=args))
        self.callback_handle = self.config_watcher.connect('changed',
                               self.on_config_changed)
        # Create the sharing server backend that keeps track of all the list
        # of items available.  Don't know whether we can just query it on the
        # fly, maybe that's a better idea.
        self.backend = SharingManagerBackend()
        # We can turn it on dynamically but if it's not too much work we'd
        # like to get these before so that turning it on and off is not too
        # onerous?
        self.backend.start_tracking()
        # Enable sharing if necessary.
        self.twiddle_sharing()

    def on_config_changed(self, obj, key, value):
        # We actually know what's changed but it's so simple let's not bother.
        eventloop.add_urgent_call(self.twiddle_sharing, "twiddle sharing")

    def twiddle_sharing(self):
        sharing = app.config.get(prefs.SHARE_MEDIA)
        discoverable = app.config.get(prefs.SHARE_DISCOVERABLE)

        if sharing != self.sharing:
            if sharing:
                # TODO: if this didn't work, should we set a timer to retry
                # at some point in the future?
                if not self.enable_sharing():
                    return
            else:
                if self.discoverable:
                    self.disable_discover()
                self.disable_sharing()

        # Short-circuit: if we have just disabled the share, then we don't
        # need to check the discoverable bits since it is not relevant, and
        # would already have been disabled anyway.
        if not self.sharing:
            return

        if discoverable != self.discoverable:
            if discoverable:
                self.enable_discover()
            else:
                self.disable_discover()

    def get_address(self):
        server_address = (None, None)
        try:
            server_address = self.server.server_address
        except AttributeError:
            pass
        return server_address

    def enable_discover(self):
        name = app.config.get(prefs.SHARE_NAME)
        # At this point the server must be available, because we'd otherwise
        # have no clue what port to register for with Bonjour.
        address, port = self.server.server_address
        self.mdns_callback = libdaap.install_mdns(name, port=port)
        # not exactly but close enough: it's not actually until the
        # processing function gets called.
        self.discoverable = True
        # Reload the server thread: if we are only toggling between it
        # being advertised, then the server loop is already running in
        # the select() loop and won't know that we need to process the
        # registration.
        self.w.send(self.CMD_NOP)

    def disable_discover(self):
        self.discoverable = False
        libdaap.uninstall_mdns(self.mdns_callback)

    def server_thread(self):
        server_fileno = self.server.fileno()
        while True:
            try:
                rset = [server_fileno, self.r]
                refs = []
                if self.discoverable:
                    refs += self.mdns_callback.get_refs()
                rset += refs
                r, w, x = select.select(rset, [], [])
                for i in r:
                    if i in refs:
                        self.mdns_callback(i)
                        continue
                    if server_fileno == i:
                        self.server.handle_request()
                        continue
                    if self.r == i:
                        cmd = self.r.recv(1024)
                        print 'CMD', cmd
                        if cmd == self.CMD_QUIT:
                            return
                        elif cmd == self.CMD_NOP:
                            print 'RELOAD'
                            continue
                        else:
                            raise 
            except select.error, (err, errstring):
                if err == errno.EINTR:
                    continue 
                else:
                    pass
            # XXX How to pass error, send message to the backend/frontend?
            except:
                pass

    def enable_sharing(self):
        name = app.config.get(prefs.SHARE_NAME)
        self.server = libdaap.make_daap_server(self.backend, name=name)
        if not self.server:
            self.sharing = False
            return
        self.thread = threading.Thread(target=thread_body,
                                       args=[self.server_thread],
                                       name='DAAP Server Thread')
        self.thread.start()
        self.sharing = True

        return self.sharing

    def disable_sharing(self):
        self.sharing = False
        # What to do in case of socket error here?
        self.w.send(self.CMD_QUIT)
        del self.thread
        del self.server

    def shutdown(self):
        if self.sharing:
            self.disable_sharing()
