"""A running wrapper must retain its parsed body after the file is rewritten."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIBRARIES = {'run-identity.sh', 'outcome-taxonomy.sh'}


class WrapperFreezeTests(unittest.TestCase):
    def test_every_executable_wrapper_survives_in_place_rewrite(self):
        for original in sorted((ROOT / 'scripts').glob('*.sh')):
            if original.name in LIBRARIES:
                continue
            with self.subTest(wrapper=original.name), tempfile.TemporaryDirectory() as tmp:
                text = original.read_text()
                self.assertIn('main() {', text)
                self.assertEqual(text.splitlines()[-1], 'main "$@"; exit $?')
                # The barrier stops a real bash process inside the production function.
                # It then returns a distinctive exit before vendor calls can happen.
                text = text.replace('main() {', 'main() {\nprintf "READY\\n"\nread -r barrier\nprintf "ORIGINAL:%s\\n" "$1"\nreturn 37\n', 1)
                script = Path(tmp) / original.name
                script.write_text(text)
                proc = subprocess.Popen(['bash', str(script), 'argument'], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(proc.stdout.readline(), 'READY\n')
                    # Reproduce the original symptom: truncate and rewrite the very file bash opened.
                    script.write_text('#!/bin/bash\nprintf "REPLACEMENT\\n"\nexit 99\n')
                    out, err = proc.communicate('continue\n', timeout=5)
                    self.assertEqual(proc.returncode, 37, err)
                    self.assertEqual(out, 'ORIGINAL:argument\n')
                finally:
                    if proc.poll() is None:
                        proc.kill()
                        proc.communicate()


if __name__ == '__main__':
    unittest.main()
