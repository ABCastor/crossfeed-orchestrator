"""Intercept the embedded media transport. No network or credentials are used."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/gemini-media.sh'


class MediaCleanupTests(unittest.TestCase):
    def run_transport(self, state='ACTIVE', *, expired=False, generation_error=False, cancel=False):
        code = SCRIPT.read_text().split("<<'PY'", 1)[1].split("\n", 1)[1].split('\nPY\n', 1)[0]
        calls = []
        class Response(io.BytesIO):
            headers = {'X-Goog-Upload-URL': 'https://fake/upload'}
        def urlopen(request, timeout):
            calls.append((request.method, request.full_url, timeout))
            if request.method == 'DELETE':
                return Response(b'')
            if request.full_url.endswith('/upload'):
                return Response(json.dumps({'file': {'name': 'files/owned', 'uri': 'fake-uri', 'state': state}}).encode())
            if 'countTokens' in request.full_url:
                if expired:
                    raise TimeoutError('deadline expired')
                if cancel:
                    raise SystemExit(143)
                return Response(b'{"totalTokens":10}')
            if 'generateContent' in request.full_url:
                if generation_error:
                    raise OSError('generation failed')
                return Response(b'{"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":1},"candidates":[{"content":{"parts":[{"text":"answer"}]}}]}')
            return Response(b'{}')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            media = root / 'audio.wav'
            media.write_bytes(b'synthetic audio')
            env = {'GEMINI_MEDIA_KEY': 'fake', 'GEMINI_MEDIA_CAP_USD': '1', 'GEMINI_MEDIA_LEDGER': str(root / 'ledger')}
            def remaining_clock():
                return 9999 if expired and any('countTokens' in c[1] for c in calls) else 0
            with mock.patch.dict(os.environ, env), mock.patch('sys.argv', ['-', str(media), 'audio', 'task', 'gemini-flash-lite-latest', '600']), mock.patch('urllib.request.urlopen', side_effect=urlopen), mock.patch('time.monotonic', side_effect=remaining_clock), mock.patch('signal.signal'), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    exec(compile(code, str(SCRIPT), 'exec'), {})
                except SystemExit:
                    pass
        self.assertIn(('DELETE', 'https://generativelanguage.googleapis.com/v1beta/files/owned'), [(m, u) for m, u, _ in calls])

    def test_deletes_success_and_generation_error(self):
        self.run_transport()
        self.run_transport(generation_error=True)

    def test_deletes_processing_failure_before_upload_returns(self):
        self.run_transport(state='FAILED')

    def test_deletes_even_after_generation_deadline(self):
        self.run_transport(expired=True)

    def test_deletes_on_cancellation(self):
        self.run_transport(cancel=True)

    def test_wrapper_forwards_term_and_waits_for_remote_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wrapper = root / SCRIPT.name
            shutil.copy(SCRIPT, wrapper)
            gate = root / 'fleetctl.py'
            gate.write_text('#!/bin/sh\nexit 0\n')
            gate.chmod(0o755)
            (root / 'audio.wav').write_bytes(b'synthetic audio')
            (root / 'key').write_text('fake-only')
            (root / 'sitecustomize.py').write_text('''
import io, json, os, time, urllib.request
from pathlib import Path
root = Path(os.environ['MEDIA_TEST_ROOT'])
class Response(io.BytesIO):
    headers = {'X-Goog-Upload-URL': 'https://fake/upload'}
def urlopen(request, timeout=None):
    if request.method == 'DELETE':
        (root / 'deleted').write_text(request.full_url)
        return Response(b'')
    if request.full_url.endswith('/upload'):
        return Response(b'{"file":{"name":"files/owned","uri":"fake","state":"ACTIVE"}}')
    if 'countTokens' in request.full_url:
        (root / 'ready').touch()
        time.sleep(60)
    return Response(b'{}')
urllib.request.urlopen = urlopen
''')
            env = dict(os.environ, HOME=str(root / 'home'), TRANSCRIBE_KEY_FILE=str(root / 'key'),
                       GEMINI_MEDIA_LEDGER=str(root / 'ledger'), PYTHONPATH=str(root), MEDIA_TEST_ROOT=str(root))
            child = subprocess.Popen(['bash', str(wrapper), '--file', str(root / 'audio.wav'),
                                      '--modality', 'audio', '--prompt', 'task'], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                until = time.monotonic() + 5
                while not (root / 'ready').exists() and child.poll() is None and time.monotonic() < until:
                    time.sleep(.02)
                self.assertTrue((root / 'ready').exists())
                child.send_signal(signal.SIGTERM)
                stdout, stderr = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 143, stderr)
                self.assertEqual(stdout, b'')
                self.assertTrue((root / 'deleted').exists(), stderr)
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)

if __name__ == '__main__':
    unittest.main()
