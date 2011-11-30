import os
import time
import Queue

from miro import app
from miro import moviedata
from miro import subprocessmanager
from miro import workerprocess
from miro.plat import resources
from miro.test.framework import EventLoopTest

# setup some test messages/handlers
class TestSubprocessHandler(subprocessmanager.SubprocessHandler):
    def __init__(self):
        subprocessmanager.SubprocessHandler.__init__(self)

    def on_startup(self):
        SawEvent("startup").send_to_main_process()

    def on_shutdown(self):
        SawEvent("shutdown").send_to_main_process()

    def on_restart(self):
        SawEvent("restart").send_to_main_process()

    def handle_ping(self, msg):
        Pong().send_to_main_process()

    def handle_force_exception(self, msg):
        1/0

class TestSubprocessResponder(subprocessmanager.SubprocessResponder):
    def __init__(self):
        subprocessmanager.SubprocessResponder.__init__(self)
        self.events_saw = []
        self.subprocess_events_saw = []
        self.pong_count = 0
        self.break_on_pong = False
        self.subprocess_ready = False

    def on_startup(self):
        self.events_saw.append("startup")

    def on_restart(self):
        self.events_saw.append("restart")

    def on_shutdown(self):
        self.events_saw.append("shutdown")

    def handle_pong(self, msg):
        if self.break_on_pong:
            1/0
        self.pong_count += 1

    def handle_saw_event(self, msg):
        self.subprocess_events_saw.append(msg.event)
        if msg.event == 'startup':
            self.subprocess_ready = True

class TestMessage(subprocessmanager.SubprocessMessage):
    pass

class Ping(TestMessage):
    pass

class ForceException(TestMessage):
    pass

class Pong(subprocessmanager.SubprocessResponse):
    pass

class SawEvent(subprocessmanager.SubprocessResponse):
    def __init__(self, event):
        self.event = event

class SlowRunningTask(workerprocess.TaskMessage):
    """Task sent to the worker process that should do nothing except take a
    bunch of time.
    """
    priority = -10

# Actual tests go below here

class SubprocessManagerTest(EventLoopTest):
    # FIXME: we should have a better way of waiting for the subprocess to do
    # things, than calling runEventLoop() with an arbitrary timeout.

    def setUp(self):
        EventLoopTest.setUp(self)

        self.responder = TestSubprocessResponder()
        self.subprocess = subprocessmanager.SubprocessManager(TestMessage,
                self.responder, TestSubprocessHandler)
        self.subprocess.start()
        self._wait_for_subprocess_ready()

    def tearDown(self):
        self.subprocess.shutdown()
        EventLoopTest.tearDown(self)

    def _wait_for_subprocess_ready(self, timeout=6.0):
        """Wait for the subprocess to startup."""

        start = time.time()
        while True:
            # wait a bit for the subprocess to send us a message
            self.runEventLoop(0.1, timeoutNormal=True)
            if self.responder.subprocess_ready:
                return
            if time.time() - start > timeout:
                self.subprocess.process.terminate()
                raise AssertionError("subprocess didn't startup in %s secs",
                        timeout)

    def test_startup(self):
        # test that we startup the process
        self.assert_(self.subprocess.process.poll() is None)
        self.assert_(self.subprocess.thread.is_alive())

    def test_quit(self):
        # test asking processes to quit nicely
        thread = self.subprocess.thread
        process = self.subprocess.process
        self.subprocess.send_quit()
        # give a bit of time to let things quit
        self.runEventLoop(0.3, timeoutNormal=True)
        self.assert_(not thread.is_alive())
        self.assert_(process.poll() is not None)
        self.assertEquals(process.returncode, 0)
        self.assert_(not self.subprocess.is_running)

    def test_send_and_receive(self):
        # test sending and receiving messages

        # send a bunch of pings
        Ping().send_to_process()
        Ping().send_to_process()
        Ping().send_to_process()
        # allow some time for the responses to come back
        self.runEventLoop(0.1, timeoutNormal=True)
        # check that we got a pong for each ping
        self.assertEquals(self.responder.pong_count, 3)

    def test_event_callbacks(self):
        # test that we get event callbacks

        # check that on_startup() gets called on both sides
        self.runEventLoop(0.1, timeoutNormal=True)
        self.assertEqual(self.responder.events_saw, ['startup'])
        self.assertEqual(self.responder.subprocess_events_saw, ['startup'])

        # check that on_restart() gets called on both sides
        self.responder.events_saw = []
        self.responder.subprocess_events_saw = []
        self.subprocess.process.terminate()
        self.responder.subprocess_ready = False
        self._wait_for_subprocess_ready()
        # the subprocess should see a startup
        self.assertEqual(self.responder.subprocess_events_saw, ['startup'])
        # the main process should see a startup and a restart
        self.assertEqual(self.responder.events_saw, ['startup', 'restart'])

        # check that on_shutdown() gets called on both sides
        self.responder.events_saw = []
        self.responder.subprocess_events_saw = []
        self.subprocess.shutdown()
        self.runEventLoop(0.2, timeoutNormal=True)
        self.assertEqual(self.responder.subprocess_events_saw, ['shutdown'])
        self.assertEqual(self.responder.events_saw, ['shutdown'])

    def test_restart(self):
        # test that we restart process when the quit unexpectedly
        old_pid = self.subprocess.process.pid
        old_thread = self.subprocess.thread
        self.subprocess.process.terminate()
        # wait a bit for the subprocess to quit then restart
        self.responder.subprocess_ready = False
        self._wait_for_subprocess_ready()
        # test that process #1 has been restarted
        self.assert_(self.subprocess.is_running)
        self.assert_(self.subprocess.process.poll() is None)
        self.assert_(self.subprocess.thread.is_alive())
        self.assertNotEqual(old_pid, self.subprocess.process.pid)
        # test that the original thread is gone
        self.assert_(not old_thread.is_alive())

    def test_subprocess_exception(self):
        # check that subprocess handler exceptions don't break things
        original_pid = self.subprocess.process.pid
        # sending this message causes the subprocess handler to throw an
        # exception
        ForceException().send_to_process()
        app.controller.failed_soft_okay = True
        # check that we handled the exception with failed_soft
        self.runEventLoop(0.1, timeoutNormal=True)
        self.assertEquals(app.controller.failed_soft_count, 1)
        # check that we're didn't restart the process
        self.assertEquals(original_pid, self.subprocess.process.pid)
        # check that we can still send messages
        Ping().send_to_process()
        self.runEventLoop(0.1, timeoutNormal=True)
        self.assertEquals(self.responder.pong_count, 1)

class UnittestWorkerProcessHandler(workerprocess.WorkerProcessHandler):
    def handle_feedparser_task(self, msg):
        if msg.html == 'FORCE EXCEPTION':
            raise ValueError("Simulated Exception")
        else:
            return workerprocess.WorkerProcessHandler.handle_feedparser_task(
                    self, msg)

    def handle_slow_running_task(self, msg):
        time.sleep(0.5)
        return None

class WorkerProcessTest(EventLoopTest):
    """Test our worker process."""
    def setUp(self):
        EventLoopTest.setUp(self)
        # override the normal handler class with our own
        workerprocess._subprocess_manager.handler_class = (
                UnittestWorkerProcessHandler)
        self.reset_results()

    def reset_results(self):
        self.result = self.error = None

    def callback(self, msg, result):
        self.result = result
        self.stopEventLoop(abnormal=False)

    def errback(self, msg, error):
        self.error = error
        self.stopEventLoop(abnormal=False)

class FeedParserTest(WorkerProcessTest):
    def send_feedparser_task(self):
        # send feedparser successfully parsing a feed
        path = os.path.join(resources.path("testdata/feedparsertests/feeds"),
            "http___feeds_miroguide_com_miroguide_featured.xml")
        html = open(path).read()
        msg = workerprocess.FeedparserTask(html)
        workerprocess.send(msg, self.callback, self.errback)

    def check_successful_result(self):
        if self.error is not None:
            raise self.error
        self.assertNotEquals(self.result, None)
        # just do some very basic test to see if the result is correct
        if self.result['bozo']:
            raise AssertionError("Feedparser parse error: %s",
                    self.result['bozo_exception'])

    def test_feedparser_success(self):
        # test feedparser successfully parsing a feed
        workerprocess.startup()
        self.send_feedparser_task()
        self.runEventLoop(4.0)
        self.check_successful_result()

    def test_feedparser_error(self):
        # test feedparser failing to parse a feed
        workerprocess.startup()
        msg = workerprocess.FeedparserTask('FORCE EXCEPTION')
        workerprocess.send(msg, self.callback, self.errback)
        self.runEventLoop(4.0)
        self.assertEquals(self.result, None)
        self.assert_(isinstance(self.error, ValueError))

    def test_crash(self):
        # force a crash of our subprocess right after we send the task
        workerprocess.startup()
        original_pid = workerprocess._subprocess_manager.process.pid
        self.send_feedparser_task()
        workerprocess._subprocess_manager.process.terminate()
        self.runEventLoop(4.0)
        # check that we really restarted the subprocess
        self.assertNotEqual(original_pid,
                workerprocess._subprocess_manager.process.pid)
        self.check_successful_result()

    def test_queue_before_start(self):
        # test sending tasks before we start the worker process

        # since our process hasn't started, this should just queue up things
        self.send_feedparser_task()
        # start the process and check that we process the task
        workerprocess.startup()
        self.runEventLoop(4.0)
        self.check_successful_result()

class MovieDataTest(WorkerProcessTest):
    def check_successful_result(self):
        # just do some very basic test to see if the result is correct
        if self.error is not None:
            raise self.error
        if not isinstance(self.result, dict):
            raise TypeError(self.result)

    def check_movie_data_call(self, filename, file_type, duration):
        source_path = resources.path("testdata/metadata/" + filename)
        msg = workerprocess.MovieDataProgramTask(source_path, self.tempdir)
        workerprocess.send(msg, self.callback, self.errback)
        self.runEventLoop(4.0)
        self.check_successful_result()
        self.assertEquals(self.result['source_path'], source_path)
        self.assertEquals(self.result['file_type'], file_type)
        self.assertEquals(self.result['duration'], duration)
        if file_type == 'video':
            screenshot_name = os.path.basename(source_path) + '.png'
            self.assertEquals(self.result['screenshot_path'],
                              os.path.join(self.tempdir, screenshot_name))
        else:
            self.assertEquals(self.result['screenshot_path'], None)
        self.reset_results()

    def test_movie_data_worker_process(self):
        workerprocess.startup()
        self.check_movie_data_call('mp3-0.mp3', 'audio', 1044)
        self.check_movie_data_call('mp3-1.mp3', 'audio', 1044)
        self.check_movie_data_call('mp3-2.mp3', 'audio', 1044)
        self.check_movie_data_call('webm-0.webm', 'video', 434)
        self.check_movie_data_call('drm.m4v', 'other', None)

class MutagenTest(WorkerProcessTest):
    def check_successful_result(self):
        # just do some very basic test to see if the result is correct
        if self.error is not None:
            raise self.error
        if not isinstance(self.result, dict):
            raise TypeError(self.result)

    def check_mutagen_call(self, filename, file_type, duration, title,
                           has_cover_art):
        source_path = resources.path("testdata/metadata/" + filename)
        msg = workerprocess.MutagenTask(source_path, self.tempdir)
        workerprocess.send(msg, self.callback, self.errback)
        self.runEventLoop(4.0)
        self.check_successful_result()
        self.assertEquals(self.result['source_path'], source_path)
        self.assertEquals(self.result['file_type'], file_type)
        self.assertEquals(self.result['duration'], duration)
        self.assertEquals(self.result['title'], title)
        if has_cover_art:
            self.assertNotEquals(self.result['cover_art_path'], None)
        else:
            self.assertEquals(self.result['cover_art_path'], None)
        self.reset_results()

    def test_mutagen_worker_process(self):
        workerprocess.startup()
        self.check_mutagen_call('mp3-0.mp3', 'audio', 1055,
                                   'Invisible Walls', False)
        self.check_mutagen_call('mp3-1.mp3', 'audio', 1055, 'Race Lieu',
                                False)
        self.check_mutagen_call('mp3-2.mp3', 'audio', 1066,
                                   '#426: Tough Room 2011', False)
        self.check_mutagen_call('drm.m4v', 'video', 2668832, 'Thinkers',
                                True)

class WorkerSystemTest(EventLoopTest):
    """Contains tests for the worker process system as a whole
    """

    def setUp(self):
        EventLoopTest.setUp(self)
        self.stop_on_result_count = -1
        self.callback_data = []
        self.errback_data = []
        workerprocess._subprocess_manager.handler_class = (
                UnittestWorkerProcessHandler)

    def callback(self, msg, result):
        self.callback_data.append((msg, result))
        self.check_stop_event_loop()

    def errback(self, msg, error):
        self.errback_data.append((msg, error))
        self.check_stop_event_loop()

    def check_stop_event_loop(self):
        """Check if enough callbacks/errbacks have been called that we should
        stop the event loop
        """
        total_results = len(self.callback_data) + len(self.errback_data)
        if total_results >= self.stop_on_result_count:
            self.stopEventLoop(abnormal=False)

    def wait_for_results(self, count):
        self.stop_on_result_count = count
        self.runEventLoop(4.0)

    def _wait_for_subprocess_ready(self, timeout=6.0):
        """Wait for the subprocess to startup."""

        start = time.time()
        while True:
            # wait a bit for the subprocess to send us a message
            self.runEventLoop(0.1, timeoutNormal=True)
            if workerprocess._subprocess_manager.responder.worker_ready:
                return
            if time.time() - start > timeout:
                raise AssertionError("subprocess didn't startup in %s secs",
                        timeout)

    def send_slow_processing_task(self):
        workerprocess.send(SlowRunningTask(), self.callback, self.errback)

    def send_feedparser_task(self):
        # send feedparser successfully parsing a feed
        path = os.path.join(resources.path("testdata/feedparsertests/feeds"),
            "http___feeds_miroguide_com_miroguide_featured.xml")
        html = open(path).read()
        msg = workerprocess.FeedparserTask(html)
        workerprocess.send(msg, self.callback, self.errback)

    def send_mutagen_task(self, filename):
        path = resources.path("testdata/metadata/" + filename)
        msg = workerprocess.MutagenTask(path, self.tempdir)
        workerprocess.send(msg, self.callback, self.errback)

    def send_moviedata_task(self, filename):
        path = resources.path("testdata/metadata/" + filename)
        msg = workerprocess.MovieDataProgramTask(path, self.tempdir)
        workerprocess.send(msg, self.callback, self.errback)

    def check_result_classes(self, *class_list):
        result_classes = [msg.__class__ for msg, result in self.callback_data]
        self.assertEquals(result_classes, list(class_list))

    def test_priority(self):
        # Test that tasks run with the correct priority
        workerprocess.startup(thread_count=2)
        self._wait_for_subprocess_ready()
        # send a bunch of tasks that are slow to process
        for i in xrange(4):
            self.send_slow_processing_task()
        # wait long enough to make sure all of those tasks got started on the
        # worker process, but not long enough so that any one of them
        # completed.
        time.sleep(0.2)
        # now send feedparser task
        self.send_feedparser_task()
        # wait to get results for all the tasks.  The feedparser task should
        # have been bumped ahead of the queued SlowRunningTask tasks.
        self.wait_for_results(5)
        self.assertEquals(self.errback_data, [])
        self.check_result_classes(SlowRunningTask, SlowRunningTask,
                                  workerprocess.FeedparserTask,
                                  SlowRunningTask, SlowRunningTask)

    def test_cancel_files(self):
        # Test the CancelFileOperations message
        workerprocess.startup(thread_count=2)
        self._wait_for_subprocess_ready()
        # fill up the task queue with stuff
        for i in xrange(2):
            self.send_slow_processing_task()
        # wait long enough to make sure all of those tasks got started on the
        # worker process, but not long enough so that any one of them
        # completed.
        time.sleep(0.2)
        # send bunch of mutagen/metadata tasks
        self.send_mutagen_task('mp3-0.mp3')
        self.send_mutagen_task('mp3-1.mp3')
        self.send_moviedata_task('webm-0.webm')
        # stop tasks for some of the files
        to_cancel = [
            resources.path('testdata/metadata/mp3-0.mp3'),
            resources.path('testdata/metadata/webm-0.webm'),
        ]
        workerprocess.cancel_tasks_for_files(to_cancel)
        # wait for results.  We should get results for the 2 SlowRunningTask
        # objects and 1 of our mutagen tasks
        self.wait_for_results(3)
        self.check_result_classes(SlowRunningTask, SlowRunningTask,
                                  workerprocess.MutagenTask)
        self.assertEquals(self.callback_data[2][0].source_path,
                          resources.path('testdata/metadata/mp3-1.mp3'))
