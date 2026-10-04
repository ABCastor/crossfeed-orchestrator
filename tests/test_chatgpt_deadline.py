"""Wall budgets include ChatGPT admission and quota preflight, without live calls."""
import contextlib
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import chatgpt_runner as runner
from chatgpt_transport import Rejected


class WallDeadlineTests(unittest.TestCase):
    def test_expired_preflight_never_starts_http_child(self):
        args = SimpleNamespace(wall=600, wall_deadline=610)
        with mock.patch.object(runner.time, 'monotonic', return_value=611), \
             mock.patch.object(runner.multiprocessing, 'get_context') as context:
            with self.assertRaises(Rejected) as failure:
                runner.supervise(args, 'fixture', 'fixture', {}, io.StringIO(), [], 'fixture')
        self.assertEqual(failure.exception.code, 124)
        context.assert_not_called()

    def test_inflight_stops_at_original_deadline_after_slow_preflight(self):
        args = SimpleNamespace(wall=600, wall_deadline=610, idle=0, kill_after=2)
        context = mock.Mock()
        receiver, sender, child = mock.Mock(), mock.Mock(), mock.Mock()
        child.is_alive.return_value = False
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = child
        # Budget starts at10, preflight consumes300; first request loop at611
        # must stop rather than granting another600 seconds from310.
        with mock.patch.object(runner.time, 'monotonic', side_effect=[310,310,611]), \
             mock.patch.object(runner.multiprocessing, 'get_context', return_value=context):
            with self.assertRaises(Rejected) as failure:
                runner.supervise(args, 'fixture', 'fixture', {}, io.StringIO(), [], 'fixture')
        self.assertEqual(failure.exception.code, 124)
        receiver.poll.assert_not_called()
        receiver.close.assert_called_once()

    def test_blocking_preflight_is_interrupted_and_handler_restored(self):
        import signal
        import time
        previous = signal.getsignal(signal.SIGALRM)
        started = time.monotonic()
        with self.assertRaises(Rejected) as failure:
            with runner.preflight_budget(started + .02):
                time.sleep(1)
        self.assertEqual(failure.exception.code, 124)
        self.assertLess(time.monotonic() - started, .5)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL), (0., 0.))

    def test_preflight_cannot_swallow_wall_timeout(self):
        import time
        with self.assertRaises(Rejected) as failure:
            with runner.preflight_budget(time.monotonic() + .02):
                try:
                    time.sleep(1)
                except Rejected:
                    pass  # Catalog admission may catch a transport rejection.
        self.assertEqual(failure.exception.code, 124)

    def test_preexisting_alarm_is_restored_with_elapsed_time(self):
        import signal
        import time
        signal.setitimer(signal.ITIMER_REAL, 5)
        try:
            with runner.preflight_budget(time.monotonic() + 1):
                time.sleep(.01)
            restored, interval = signal.getitimer(signal.ITIMER_REAL)
            self.assertGreater(restored, 4.8)
            self.assertLess(restored, 5)
            self.assertEqual(interval, 0)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def test_alarm_during_arming_restores_handler(self):
        import signal
        previous = signal.getsignal(signal.SIGALRM)
        def arm(kind, seconds, *args):
            if seconds:
                signal.getsignal(signal.SIGALRM)(signal.SIGALRM, None)
        with mock.patch.object(runner.signal, 'setitimer', side_effect=arm):
            with self.assertRaises(Rejected):
                with runner.preflight_budget(runner.time.monotonic() + 1):
                    self.fail('expired budget must not enter preflight')
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)

    def test_terminal_result_crossing_deadline_is_rejected(self):
        args = SimpleNamespace(wall=600, wall_deadline=610, idle=0, kill_after=2)
        context = mock.Mock()
        receiver, sender, child = mock.Mock(), mock.Mock(), mock.Mock()
        child.is_alive.return_value = False
        context.Pipe.return_value = receiver, sender
        context.Process.return_value = child
        receiver.poll.return_value = True
        receiver.recv.return_value = ('result', {'model':'fixture', 'choices':[
            {'finish_reason':'stop', 'message':{'content':'finished late'}}]})
        with mock.patch.object(runner.time, 'monotonic', side_effect=[609.9,609.9,609.99,610.02]), \
             mock.patch.object(runner.multiprocessing, 'get_context', return_value=context):
            with self.assertRaises(Rejected) as failure:
                runner.supervise(args, 'fixture', 'fixture', {'model':'fixture'}, io.StringIO(), [], 'fixture')
        self.assertEqual(failure.exception.code, 124)
        self.assertLess(receiver.poll.call_args.args[0], .011)

    def test_deadline_is_established_before_gateway_settings(self):
        args = SimpleNamespace(wall=600, action='health', lane='fixture')
        def settings(lane):
            self.assertEqual(args.wall_deadline, 610)
            return 'fixture', 'fixture'
        with mock.patch.object(runner.time, 'monotonic', return_value=10), \
             mock.patch.object(runner, 'lane_for', return_value={'model_key':'fixture'}), \
             mock.patch.object(runner, 'settings', side_effect=settings), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.run(args), 0)

if __name__ == '__main__':
    unittest.main()
