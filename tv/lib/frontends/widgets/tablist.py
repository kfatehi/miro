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

"""Displays the list of tabs on the left-hand side of the app."""

from __future__ import with_statement # neccessary for python2.5

from hashlib import md5
try:
    import cPickle as pickle
except ImportError:
    import pickle
from contextlib import contextmanager
import logging

from miro import app
from miro import prefs
from miro import signals
from miro import messages
from miro.gtcache import gettext as _
from miro.plat import resources
from miro.frontends.widgets import playback
from miro.frontends.widgets import style
from miro.frontends.widgets import imagepool
from miro.frontends.widgets import statictabs
from miro.frontends.widgets import widgetutil
from miro.frontends.widgets import menus
from miro.frontends.widgets.tablistdnd import (FeedListDragHandler,
     FeedListDropHandler, PlaylistListDragHandler, PlaylistListDropHandler,
     MediaTypeDropHandler, DeviceDropHandler)
from miro.plat.frontends.widgets import widgetset
from miro.plat.frontends.widgets import timer

class TabInfo(object):
    """Simple Info object which holds the data for a top-level tab."""
    type = u'tab'
    is_folder = False # doesn't work like a real folder
    tall = True
    unwatched = available = 0
    autodownload_mode = u'off'
    is_directory_feed = False

    def __init__(self, name, icon_name):
        self.name = name
        self.id = u'%s-base-tab' % name
        self.icon_name = icon_name
        self.thumbnail = resources.path('images/%s.png' % icon_name)
        self.icon = widgetutil.make_surface(self.icon_name)
        self.active_icon = widgetutil.make_surface(self.icon_name + '_active')

class TabListView(widgetset.TableView):
    """TableView for a tablist."""
    draws_selection = True

    def __init__(self, renderer, table_model_class=None):
        if table_model_class is None:
            table_model_class = widgetset.TreeTableModel
        table_model = table_model_class('object', 'boolean', 'integer')
        # columns are:
        # - the tab_info object
        # - should the tab should be blinking?
        # - should we draw an uploading icon?  -1: no, 0-7: frame to draw
        widgetset.TableView.__init__(self, table_model)
        self.column = widgetset.TableColumn('tab', renderer, data=0, blink=1,
                updating_frame=2)
        self.column.set_min_width(renderer.MIN_WIDTH)
        self.add_column(self.column)
        self.set_show_headers(False)
        self.set_gradient_highlight(True)
        self.set_background_color(style.TAB_LIST_BACKGROUND_COLOR)
        self.set_fixed_height(False)
        self.set_auto_resizes(True)

    def append_tab(self, tab_info):
        """Add a new tab with no parent."""
        return self.model.append(tab_info, False, -1)

    def append_child_tab(self, parent_iter, tab_info):
        """Add a new tab with a parent."""
        return self.model.append_child(parent_iter, tab_info, False, -1)

    def update_tab(self, iter_, tab_info):
        """A TabInfo has changed."""
        self.model.update_value(iter_, 0, tab_info)

    def blink_tab(self, iter_):
        """Draw attention to a tab, specified by its Iter."""
        self.model.update_value(iter_, 1, True)

    def unblink_tab(self, iter_):
        """Stop drawing attention to tab specified by Iter."""
        self.model.update_value(iter_, 1, False)

    def pulse_updating_image(self, iter_):
        frame = self.model[iter_][2]
        self.model.update_value(iter_, 2, (frame + 1) % 12)
        self.model_changed()

    def stop_updating_image(self, iter_):
        self.model.update_value(iter_, 2, -1)
        self.model_changed()

class TabUpdaterMixin(object):
    def __init__(self):
        self.updating_animations = {}

    def start_updating(self, id_):
        # The spinning wheel is constantly updating the cell value, between
        # validating the cell value for the drag and drop and the actual drop
        # the cell value most likely changes, and some GUI toolkits may get
        # confused.
        #
        # We'll let the underlying platform code figure out what's the best
        # thing to do here.
        self.view.set_volatile(True)
        if id_ in self.updating_animations:
            return
        timer_id = timer.add(0, self.pulse_updating_animation, id_)
        self.updating_animations[id_] = timer_id

    def stop_updating(self, id_):
        self.view.set_volatile(False)
        if id_ not in self.updating_animations:
            return
        self.view.stop_updating_image(self.iter_map[id_])
        timer_id = self.updating_animations.pop(id_)
        timer.cancel(timer_id)

    def pulse_updating_animation(self, id_):
        try:
            iter_ = self.iter_map[id_]
        except KeyError:
            # feed was removed
            del self.updating_animations[id_]
            return
        self.view.pulse_updating_image(iter_)
        timer_id = timer.add(0.1, self.pulse_updating_animation, id_)
        self.updating_animations[id_] = timer_id

class TabBlinkerMixin(object):
    def blink_tab(self, tab_id):
        self.show_auto_tab(tab_id)
        self.view.blink_tab(self.iter_map[tab_id])
        timer.add(1, self._unblink_tab, tab_id)

    def _unblink_tab(self, tab_id):
        # double check that the tab still exists
        if tab_id in self.iter_map:
            self.view.unblink_tab(self.iter_map[tab_id])

class TabList(signals.SignalEmitter):
    """Handles a list of tabs on the left-side of Miro.
    
    Signals:
        tab-added: a tab has been added to this list; no parameters.
        moved-tabs-to-list(destination): tabs have been moved to destination
    """
    def __init__(self):
        signals.SignalEmitter.__init__(self)
        self.create_signal('tab-added')
        self.create_signal('moved-tabs-to-list')
        self.create_signal('row-collapsed')
        self.view = self._make_view()
        self.setup_view()
        self.iter_map = {}
        self._removing = 0
        self._adding = 0

    @contextmanager
    def removing(self):
        """For removing one or more tabs - delays updates until all changes
        finish.
        """
        self._removing += 1
        try:
            yield
        finally:
            self._removing -= 1
        if not self._adding and not self._removing:
            self.view.model_changed()

    @contextmanager
    def adding(self):
        """For adding one or more tabs; signals tab-added when all (potentially
        nested) tab-add operations are finished.
        """
        self._adding += 1
        try:
            yield
        finally:
            self._adding -= 1
        if not self._adding:
            self.emit('tab-added')
        if not self._adding and not self._removing:
            self.view.model_changed()

    @contextmanager
    def preserving_expanded_rows(self):
        """Prevent expanded rows from being collapsed by changes. Implementation
        does not currently handle nesting.
        """
        expanded_rows = (id_ for id_, iter_ in self.iter_map.iteritems() if
            id_ == self.info.id or self.view.is_row_expanded(iter_))
        try:
            yield
        finally:
            for id_ in expanded_rows:
                self.view.set_row_expanded(self.iter_map[id_], True)

    def build_tabs(self):
        """Build any standard tabs; for non-static tabs, this is a pass."""

    def add(self, tab):
        with self.adding():
            iter_ = self.view.append_tab(tab)
            self.iter_map[tab.id] = iter_

    def extend(self, tabs):
        with self.adding():
            for tab in tabs:
                self.add(tab)

    def remove(self, name):
        iter_ = self.iter_map.pop(name)
        self.view.model.remove(iter_)

    def get_tab(self, name):
        return self.view.model[self.iter_map[name]][0]

    def setup_view(self):
        self.view.connect_weak('key-press', self.on_key_press)

    def on_key_press(self, view, key, mods):
        if app.playback_manager.is_playing:
            return playback.handle_key_press(key, mods)

class StaticTabList(TabList):
    """Handles the static tabs (the tabs on top that are always the same)."""
    def __init__(self):
        TabList.__init__(self)
        self.type = u'static'

    def _make_view(self):
        view = TabListView(style.StaticTabRenderer())
        view.allow_multiple_select(False)
        return view

    def build_tabs(self):
        self.add(statictabs.SearchTab())

    def get_default(self):
        """Returns an iter pointing to Video Search."""
        return self.view.model.first_iter()

class LibraryTabList(TabBlinkerMixin, TabList):
    """Handles all Library related tabs - Video, Audio, Downloading..."""
    def __init__(self):
        TabList.__init__(self)
        self.type = u'library'
        self.auto_tabs = {}
        self.auto_tabs_to_show = set()

    def _make_view(self):
        view = TabListView(style.StaticTabRenderer())
        view.allow_multiple_select(False)
        view.set_drag_dest(MediaTypeDropHandler())
        view.connect('selection-changed', self.on_selection_changed)
        view.connect('deselected', self.on_deselected)
        return view

    def build_tabs(self):
        self.extend([
            statictabs.ChannelGuideTab(),
            statictabs.VideoLibraryTab(),
            statictabs.AudioLibraryTab(),
        ])
        self.auto_tabs.update({'downloading': statictabs.DownloadsTab(),
                               'converting': statictabs.ConvertingTab(),
                               'others': statictabs.OthersTab()})

    def update_auto_tab_count(self, name, count):
        if count > 0:
            self.auto_tabs_to_show.add(name)
            self.show_auto_tab(name)
        elif name in self.iter_map:
            self.auto_tabs_to_show.discard(name)
            self.remove_auto_tab_if_not_selected(name)

    def show_auto_tab(self, name):
        if name not in self.iter_map:
            self.add(self.auto_tabs[name])

    def remove_auto_tab_if_not_selected(self, name):
        if name in app.tabs.selected_ids:
            return
        self.remove(name)

    def on_deselected(self, view):
        """deselected is a more specific signal that selection-changed, to
        simplify sending selection signals while handling selection-changed."""
        for name in (set(self.auto_tabs).intersection(self.iter_map) -
                     self.auto_tabs_to_show):
            self.remove_auto_tab_if_not_selected(name)

    def on_selection_changed(self, view):
        self.on_deselected(view)
        if not app.config.get(prefs.MUSIC_TAB_CLICKED):
            for iter_ in view.get_selection():
                if view.model[iter_][0].id == 'music':
                    app.widgetapp.music_tab_clicked()

    def update_download_count(self, count, non_downloading_count):
        self.update_count('downloading', 'downloading', count,
                          non_downloading_count)

    def update_converting_count(self, running_count, other_count):
        self.update_count('converting', 'converting', running_count,
                          other_count)

    def update_others_count(self, count):
        self.update_count('others', 'others', count) # second param no special
                                                     # meaning for this case... ?

    def update_new_video_count(self, count):
        self.update_count('videos', 'unwatched', count)

    def update_new_audio_count(self, count):
        self.update_count('music', 'unwatched', count)

    def update_count(self, key, attr, count, other_count=0):
        if key in self.auto_tabs:
            self.update_auto_tab_count(key, count+other_count)
        if key in self.iter_map:
            iter_ = self.iter_map[key]
            tab = self.view.model[iter_][0]
            setattr(tab, attr, count)
            self.view.update_tab(iter_, tab)
        self.view.model_changed()

class HideableTabList(TabList):
    """A type of tablist which nests under a base tab.  Connect,
    Sources/Sites/Guides, Stores, Feeds, and Playlists are all of this type.
    """

    ALLOW_MULTIPLE = True

    render_class = style.TabRenderer
    type = NotImplemented
    name = NotImplemented
    icon_name = NotImplemented

    def __init__(self):
        TabList.__init__(self)
        self.create_signal('tab-name-changed')
        self.info = TabInfo(self.name, self.icon_name)
        TabList.add(self, self.info)
        self.view.model_changed()
        self.expand_root = False

    def _make_view(self):
        view = TabListView(self.render_class())
        view.allow_multiple_select(self.ALLOW_MULTIPLE)
        view.connect('row-expanded', self.on_row_expanded_change, True)
        view.connect('row-collapsed', self.on_row_expanded_change, False)
        view.connect('selection-changed', self.on_selection_changed)
        view.set_context_menu_callback(self.on_context_menu)
        return view

    def on_context_menu(self, table_view):
        raise NotImplementedError

    def init_info(self, info):
        raise NotImplementedError

    def setup_list(self, message):
        """Called during startup to set up a newly-created list."""
        logging.debug('************* setup_list: %s', self.type)
        root_expanded = message.root_expanded
        try:
            self.set_folder_expanded(self.info.id, root_expanded)
        except ValueError:
            # there are no items yet; expand it when we get one
            self.expand_root = root_expanded
        if not hasattr(message, 'toplevels'):
            # setting up a non-nestable list
            return
        with self.adding():
            for info in message.toplevels:
                self.add(info)
                if info.is_folder:
                    for child_info in message.folder_children[info.id]:
                        self.add(child_info, info.id)
                    expanded = (info.id in message.expanded_folders)
                    self.set_folder_expanded(info.id, expanded)

    def on_key_press(self, view, key, mods):
        if key == menus.DELETE:
            self.on_delete_key_pressed()
            return True
        return TabList.on_key_press(self, view, key, mods)

    def on_row_expanded_change(self, view, iter_, path, expanded):
        info = self.view.model[iter_][0]
        if info == self.info:
            message = messages.TabExpandedChange(self.type, expanded)
            message.send_to_backend()
        else:
            # non-nestable tabs don't have tablist states, so we put their root
            # nodes all in one table
            message = messages.FolderExpandedChange(self.type, info.id, expanded)
            message.send_to_backend()
        if not expanded:
            self.emit('row-collapsed', iter_, path)

    def add(self, info, parent_id=None):
        """Add a TabInfo to the list, with an optional parent (by id)."""
        with self.adding():
            if parent_id is None:
                parent_id = self.info.id
            self.init_info(info)
            if parent_id:
                parent_iter = self.iter_map[parent_id]
                iter_ = self.view.append_child_tab(parent_iter, info)
            else:
                iter_ = self.view.append_tab(info)
            self.iter_map[info.id] = iter_
        if self.expand_root:
            self.set_folder_expanded(self.info.id, True)
            self.expand_root = False

    def set_folder_expanded(self, id_, expanded):
        logging.debug('expanding %s', id_)
        logging.debug('iter: ', self.iter_map[id_])
        self.view.set_row_expanded(self.iter_map[id_], expanded)

    def update(self, info):
        self.init_info(info)
        old_name = self.view.model[self.iter_map[info.id]][0].name
        self.view.update_tab(self.iter_map[info.id], info)
        if old_name != info.name:
            self.emit('tab-name-changed', old_name, info.name)

    def remove(self, id_list):
        with self.removing():
            for id_ in id_list:
                try:
                    iter_ = self.iter_map.pop(id_)
                except KeyError:
                    # child of a tab we already deleted
                    continue
                self.forget_child_iters(iter_)
                self.view.model.remove(iter_)

    def forget_child_iters(self, parent_iter):
        model = self.view.model
        iter_ = model.child_iter(parent_iter)
        while iter_ is not None:
            id_ = model[iter_][0].id
            del self.iter_map[id_]
            iter_ = model.next_iter(iter_)

    def model_changed(self):
        self.view.model_changed()

    def get_info(self, id_):
        return self.view.model[self.iter_map[id_]][0]

    def has_info(self, id_):
        return id_ in self.iter_map

    def get_child_count(self, id_):
        count = 0
        child_iter = self.view.model.child_iter(self.iter_map[id_])
        while child_iter is not None:
            count += 1
            child_iter = self.view.model.next_iter(child_iter)
        return count

    def on_delete_key_pressed(self):
        """For subclasses to override."""
        pass

    def on_selection_changed(self, view):
        for iter_ in view.get_selection():
            if view.model[iter_][0] is self.info:
                if not view.is_row_expanded(iter_):
                    self.set_folder_expanded(self.info.id, True)
                return

class DeviceTabListHandler(object):
    def __init__(self, tablist):
        self.tablist = tablist

    def _fake_info(self, info, typ, name):
        new_data = {
            'fake': True,
            'tab_type': typ,
            'id': '%s-%s' % (info.id, typ),
            'name': name,
            'device_name': info.name,
            'icon': imagepool.get_surface(
                resources.path('images/icon-device-%s.png' % typ)),
            'active_icon': imagepool.get_surface(
                resources.path('images/icon-device-%s_active.png' % typ))
            }

        # hack to create a DeviceInfo without dealing with __init__
        di = messages.DeviceInfo.__new__(messages.DeviceInfo)
        di.__dict__ = info.__dict__.copy()
        di.__dict__.update(new_data)
        return di

    def _get_fake_infos(self, info):
        return [self._fake_info(info, 'video', _('Video')),
                self._fake_info(info, 'audio', _('Music'))]

    def _add_fake_tabs(self, info):
        with self.tablist.adding():
            for fake in self._get_fake_infos(info):
                HideableTabList.add(self.tablist,
                                    fake,
                                    info.id)
            self.tablist.set_folder_expanded(info.id, True)

    def add(self, info):
        self.tablist.add(info)
        if info.mount and not info.info.has_multiple_devices:
            self._add_fake_tabs(info)

    def extend(self, tabs):
        self.tablist.extend(tabs)

    def update(self, info):
        tablist = self.tablist
        model = tablist.view.model
        if not tablist.has_info(info.id):
            # this gets called if a sync is in progress when the device
            # disappears
            return
        if (info.mount and not info.info.has_multiple_devices and
                not tablist.get_child_count(info.id)):
            self._add_fake_tabs(info)
        elif tablist.get_child_count(info.id):
            if not info.mount:
                parent_iter = tablist.iter_map[info.id]
                next_iter = model.child_iter(parent_iter)
                while next_iter is not None:
                    iter_ = next_iter
                    next_iter = model.next_iter(next_iter)
                    model.remove(iter_)
            else: # also update the subtabs
                for fake in self._get_fake_infos(info):
                    HideableTabList.update(tablist, fake)
                    messages.DeviceChanged(fake).send_to_frontend()
        HideableTabList.update(self.tablist, info)

    def init_info(self, info):
        info.type = u'device'
        info.unwatched = info.available = 0
        if not getattr(info, 'fake', False):
            info.icon = imagepool.get_surface(
                resources.path('images/icon-device.png'))
            info.active_icon = imagepool.get_surface(
                resources.path('images/icon-device_active.png'))
            if getattr(info, 'is_updating', False):
                self.tablist.start_updating(info.id)
            else:
                self.tablist.stop_updating(info.id)

    def on_hotspot_clicked(self, view, hotspot, iter_):
        if hotspot == 'eject-device':
            info = view.model[iter_][0]
            messages.DeviceEject(info).send_to_backend()

class SharingTabListHandler(object):
    def __init__(self, tablist):
        self.tablist = tablist

    def on_hotspot_clicked(self, view, hotspot, iter_):
        if hotspot == 'eject-device':
            # Don't track this tab anymore for music.
            info = view.model[iter_][0]
            info.mount = False
            # We must stop the playback if we are playing from the same
            # share that we are ejecting from.
            host = info.host
            port = info.port
            item = app.playback_manager.get_playing_item()
            remote_item = False
            if item and item.remote:
                remote_item = True
            if remote_item and item.host == host and item.port == port:
                app.playback_manager.stop()
            # Default to select the guide.  There's nothing more to see here.
            typ, selected_tabs = app.tabs.selection
            if typ == u'connect' and (info == selected_tabs[0] or
              getattr(selected_tabs[0], 'parent_id', None) == info.id):
                app.tabs.select_guide()
            messages.SharingEject(info).send_to_backend()

    def update(self, info):
        if info.is_updating:
            self.tablist.start_updating(info.id)
        else:
            self.tablist.stop_updating(info.id)
        HideableTabList.update(self.tablist, info)

    def init_info(self, info):
        info.type = u'sharing'
        info.unwatched = info.available = 0
        video_playlist_id = unicode(md5(
                                    repr((u'video',
                                    info.host,
                                    info.port, u'video'))).hexdigest())
        audio_playlist_id = unicode(md5(
                                    repr((u'audio',
                                    info.host,
                                    info.port, u'audio'))).hexdigest())
        if info.is_folder:
            thumb_path = resources.path('images/sharing.png')
        # Checking the name instead of a supposedly unique id is ok for now
        # because
        elif info.playlist_id == u'video':
            thumb_path = resources.path('images/icon-video.png')
            info.name = _('Video')
        elif info.playlist_id == u'audio':
            thumb_path = resources.path('images/icon-audio.png')
            info.name = _('Music')
        else:
            thumb_path = resources.path('images/icon-playlist.png')
        info.icon = imagepool.get_surface(thumb_path)

class ConnectList(TabUpdaterMixin, HideableTabList):
    name = _('Connect')
    icon_name = 'icon-connect'
    type = u'connect'

    ALLOW_MULTIPLE = False

    render_class = style.ConnectTabRenderer

    def __init__(self):
        HideableTabList.__init__(self)
        TabUpdaterMixin.__init__(self)
        self.info_class_map = {
            messages.DeviceInfo: DeviceTabListHandler(self),
            messages.SharingInfo: SharingTabListHandler(self),
            TabInfo: None,
            }
        self.view.connect_weak('hotspot-clicked', self.on_hotspot_clicked)
        self.view.connect_weak('row-clicked', self.on_row_clicked)
        self.view.set_drag_dest(DeviceDropHandler(self))

    def on_row_expanded_change(self, view, iter_, path, expanded):
        info = self.view.model[iter_][0]
        if info is self.info:
            HideableTabList.on_row_expanded_change(self, view, iter_, path, expanded)

    def on_delete_key_pressed(self):
        # neither handler deals with this
        return

    def on_context_menu(self, view):
        # neither handle deals with this
        return []

    def on_row_clicked(self, view, iter_):
        info = self.view.model[iter_][0]
        handler = self.info_class_map[type(info)]
        if hasattr(handler, 'on_row_clicked'):
            return handler.on_row_clicked(view, iter_)

    def on_hotspot_clicked(self, view, hotspot, iter_):
        info = self.view.model[iter_][0]
        handler = self.info_class_map[type(info)]
        return handler.on_hotspot_clicked(view, hotspot, iter_)

    def init_info(self, info):
        if info is self.info:
            return
        handler = self.info_class_map[type(info)]
        return handler.init_info(info)

    def add(self, info, parent_id=None):
        handler = self.info_class_map[type(info)]
        if hasattr(handler, 'add'):
            handler.add(info) # device doesn't use the parent_id
        else:
            HideableTabList.add(self, info, parent_id)

    def update(self, info):
        handler = self.info_class_map[type(info)]
        if hasattr(handler, 'update'):
            handler.update(info)
        else:
            HideableTabList.update(self, info)

class SiteList(HideableTabList):
    type = u'site'
    name = _('Sources')
    icon_name = 'icon-source'

    ALLOW_MULTIPLE = True

    def __init__(self):
        HideableTabList.__init__(self)
        self.default_info = None

    def on_delete_key_pressed(self):
        app.widgetapp.remove_current_site()

    def init_info(self, info):
        if info is self.info:
            return
        if info.favicon:
            thumb_path = info.favicon
        else:
            if info.favicon:
                thumb_path = info.favicon
            else:
                thumb_path = resources.path('images/icon-source.png')
                info.active_icon = imagepool.get_surface(
                    resources.path('image/icon-source_active.png'))
        surface = imagepool.get_surface(thumb_path)
        if surface.width > 16 or surface.height > 16:
            info.icon = imagepool.get_surface(thumb_path, size=(16, 16))
        else:
            info.icon = surface
        info.unwatched = info.available = 0
        info.type = self.type

    def on_context_menu(self, table_view):
        selected_rows = [table_view.model[iter_][0] for iter_ in
                table_view.get_selection()]
        if len(selected_rows) == 1:
            if selected_rows[0].type == u'tab':
                return []
            return [(_('Copy URL to clipboard'), app.widgetapp.copy_site_url),
                    (_('Rename Source'), app.widgetapp.rename_something),
                    (_('Remove Source'), app.widgetapp.remove_current_site)]
        else:
            return [
                (_('Remove Sources'), app.widgetapp.remove_current_site),
                ]

    def get_default(self):
        """Return the iter pointing to this list's default tab."""
        return self.iter_map[self.default_info.id]

class StoreList(SiteList):
    type = u'store'
    name = _('Stores')
    icon_name = 'icon-store'

    ALLOW_MULTIPLE = False

    def on_delete_key_pressed(self):
        pass # XXX: can't delete stores(?)

    def on_context_menu(self, table_view):
        selected_rows = [table_view.model[iter_][0] for iter_ in
                table_view.get_selection()]
        if len(selected_rows) == 1 and selected_rows[0].type != u'tab':
            return [
                (_('Copy URL to clipboard'), app.widgetapp.copy_site_url),
            ]
        else:
            return []

class NestedTabListMixin(object):
    """Tablist for tabs that can be put into folders (playlists and feeds)."""
    def on_context_menu(self, table_view):
        selected_rows = [table_view.model[iter_][0] for iter_ in
                table_view.get_selection()]
        if len(selected_rows) == 1:
            if selected_rows[0].type == u'tab':
                return []
            if selected_rows[0].is_folder:
                return self.make_folder_context_menu(selected_rows[0])
            else:
                return self.make_single_context_menu(selected_rows[0])
        else:
            return self.make_multiple_context_menu()

class FeedList(TabUpdaterMixin, NestedTabListMixin, HideableTabList):
    type = u'feed'
    name = _('Podcasts')
    icon_name = 'icon-podcast'

    def __init__(self):
        HideableTabList.__init__(self)
        TabUpdaterMixin.__init__(self)
        self.setup_dnd()

    def setup_dnd(self):
        self.view.set_drag_source(FeedListDragHandler())
        self.view.set_drag_dest(FeedListDropHandler(self))

    def on_delete_key_pressed(self):
        app.widgetapp.remove_current_feed()

    def init_info(self, info):
        if info is self.info:
            return
        info.type = self.type
        info.icon = imagepool.get_surface(info.tab_icon, size=(16, 16))
        if info.is_updating:
            self.start_updating(info.id)
        else:
            self.stop_updating(info.id)

    def get_feeds(self):
        return (self.view.model[i][0] for k, i in self.iter_map.iteritems()
                if k != self.info.id)

    def find_feed_with_url(self, url):
        for iter_ in self.iter_map.itervalues():
            info = self.view.model[iter_][0]
            if info is self.info:
                continue
            if info.url == url:
                return info
        return None

    def make_folder_context_menu(self, obj):
        return [
            (_('Update Podcasts In Folder'), app.widgetapp.update_selected_feeds),
            (_('Rename Podcast Folder'), app.widgetapp.rename_something),
            (_('Remove'), app.widgetapp.remove_current_feed)
        ]

    def make_single_context_menu(self, obj):
        menu = [
            (_('Update Podcast Now'), app.widgetapp.update_selected_feeds)
        ]

        menu.append((_('Rename'), app.widgetapp.rename_something))
        if not obj.has_original_title:
            menu.append((_('Revert Podcast Name'),
                         app.widgetapp.revert_feed_name))
        menu.append((_('Settings'), app.widgetapp.feed_settings))
        menu.append((_('Copy URL to clipboard'), app.widgetapp.copy_feed_url))
        menu.append((_('Remove'), app.widgetapp.remove_current_feed))
        return menu

    def make_multiple_context_menu(self):
        return [
            (_('Update Podcasts Now'), app.widgetapp.update_selected_feeds),
            (_('Remove'), app.widgetapp.remove_current_feed)
        ]

class PlaylistList(NestedTabListMixin, HideableTabList):
    type = u'playlist'
    name = _('Playlists')
    icon_name = 'icon-playlist'

    def __init__(self):
        HideableTabList.__init__(self)
        self.view.set_drag_source(PlaylistListDragHandler())
        self.view.set_drag_dest(PlaylistListDropHandler(self))

    def on_delete_key_pressed(self):
        app.widgetapp.remove_current_playlist()

    def init_info(self, info):
        if info is self.info:
            return
        if info.is_folder:
            info.icon = imagepool.get_surface(
                resources.path('images/icon-folder.png'))
        else:
            info.icon = imagepool.get_surface(
                resources.path('images/icon-playlist.png'))
            info.active_icon = imagepool.get_surface(
                resources.path('images/icon-playlist_active.png'))
        info.type = self.type
        info.unwatched = info.available = 0

    def get_playlists(self):
        return (self.view.model[i][0] for k, i in self.iter_map.iteritems()
                if k != self.info.id)

    def find_playlist_with_name(self, name):
        for iter_ in self.iter_map.itervalues():
            info = self.view.model[iter_][0]
            if info is self:
                continue
            if info.name == name:
                return info
        return None

    def make_folder_context_menu(self, obj):
        return [
            (_('Rename Playlist Folder'), app.widgetapp.rename_something),
            (_('Remove'), app.widgetapp.remove_current_playlist)
        ]

    def make_single_context_menu(self, obj):
        return [
            (_('Rename Playlist'), app.widgetapp.rename_something),
            (_('Remove'), app.widgetapp.remove_current_playlist)
        ]

    def make_multiple_context_menu(self):
        return [
            (_('Remove'), app.widgetapp.remove_current_playlist)
        ]

class TabListBox(widgetset.Scroller):
    """The widget displaying the full tab list."""
    def __init__(self):
        widgetset.Scroller.__init__(self, False, True)
        background = widgetset.SolidBackground()
        background.set_background_color((style.TAB_LIST_BACKGROUND_COLOR))
        background.add(self.build_vbox())
        self.add(background)
        self.set_background_color((style.TAB_LIST_BACKGROUND_COLOR))

    def build_vbox(self):
        vbox = widgetset.VBox()
        for widget in app.tabs.tab_list_widgets:
            vbox.pack_start(widget)
        return vbox

def all_tab_lists():
    """Return an iterable of all the tablist instances TabListManager should
    own, in no particular order.
    """
    return (StaticTabList(), LibraryTabList(), ConnectList(), SiteList(),
            StoreList(), FeedList(), PlaylistList())
