#!/usr/bin/env python3
"""Mechanical checks. CLI always emits one JSON object and exits zero."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid


if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CODE_TIMEOUT_SECONDS = 10

def result(passed, reason):
    return {'pass':bool(passed),'reason':reason}


def unique_object(pairs):
    obj = {}
    for key,value in pairs:
        if key in obj:
            raise ValueError('duplicate JSON key')
        obj[key] = value
    return obj


def read_json(text):
    return json.loads(text, object_pairs_hook=unique_object,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite JSON')))


def safe_relative(value):
    if not isinstance(value,str) or not value or '\\' in value:
        raise ValueError('invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or str(path) != value:
        raise ValueError('invalid relative path')
    return path


def no_symlinks(root):
    if not root.is_dir() or root.is_symlink():
        raise ValueError('workspace must be a real directory')
    if any(path.is_symlink() for path in root.rglob('*')):
        raise ValueError('workspace symlinks are not allowed')


def test_files(root):
    return {path.relative_to(root).as_posix():path for path in root.rglob('*.py')
            if path.name.startswith('test') or path.name.endswith('_test.py')}


def same_value(actual,expected):
    # Python considers True == 1; this benchmark deliberately does not.
    if type(actual) is not type(expected):
        return False
    if isinstance(expected,dict):
        return actual.keys() == expected.keys() and all(same_value(actual[k],v) for k,v in expected.items())
    if isinstance(expected,list):
        return len(actual) == len(expected) and all(same_value(a,b) for a,b in zip(actual,expected))
    return actual == expected


def normalize(value):
    if isinstance(value,str):
        # Defined in family prompts/docs: outer whitespace and case only.
        return re.sub(r'\s*([,:])\s*',r'\1', value.strip().casefold())
    return value


def run_isolated(script,root,input_text=None):
    environment = dict(os.environ)
    environment.pop('PYTHONPATH',None)
    environment.pop('PYTHONHOME',None)
    proc = subprocess.Popen([sys.executable,'-I','-B','-c',script],cwd=root,
                            env=environment,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                            stdin=subprocess.PIPE if input_text is not None else None,
                            start_new_session=True,text=True)
    try:
        stdout, stderr = proc.communicate(input=input_text,timeout=CODE_TIMEOUT_SECONDS)
        return proc.returncode,stdout
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            proc.stdout.close()
            proc.stderr.close()
        return None
    finally:
        # Clean up descendants, including ones that closed their inherited pipes.
        try:
            os.killpg(proc.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass


def typed_value(value):
    if isinstance(value,dict):
        payload = {key:typed_value(item) for key,item in value.items()}
    elif isinstance(value,list):
        payload = [typed_value(item) for item in value]
    else:
        payload = value
    return {'type':type(value).__name__,'value':payload}


def check_behavior(workspace,check):
    function, cases, expected = check.get('function'),check.get('cases'),check.get('expected')
    if (not isinstance(function,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',function)
            or not isinstance(cases,list) or not cases or not all(isinstance(args,list) for args in cases)
            or not isinstance(expected,list) or len(cases) != len(expected)):
        return result(False,'invalid behavioral cases')
    with tempfile.TemporaryDirectory(prefix='crossfeed-behavior-') as temp:
        root = Path(temp)
        candidate = root/'candidate'
        shutil.copytree(workspace,candidate)
        marker = 'CROSSFEED_VALUES_'+uuid.uuid4().hex
        # This interpreter gets inputs, never expected values or assertions.
        # Bind serialization helpers before importing candidate code.
        script = (
            'import sys, json\n'
            '_type, _dict, _list = type, dict, list\n'
            '_id, _set = id, set\n'
            '_loads, _dumps, _print = json.loads, json.dumps, print\n'
            'payload = _loads(sys.stdin.read())\n'
            'def encode(value):\n'
            '    if _type(value) is _dict:\n'
            '        if any(_type(key) is not str for key in value):\n'
            '            raise TypeError("result dict keys must be strings")\n'
            '        data = {key: encode(item) for key,item in value.items()}\n'
            '    elif _type(value) is _list:\n'
            '        data = [encode(item) for item in value]\n'
            '    elif _type(value) in (int, str, bool, float, type(None)):\n'
            '        data = value\n'
            '    else:\n'
            '        raise TypeError("unsupported result type")\n'
            '    return {"type": _type(value).__name__, "value": data}\n'
            'def containers(value):\n'
            '    if _type(value) is _dict:\n'
            '        children = value.values()\n'
            '    elif _type(value) is _list:\n'
            '        children = value\n'
            '    else:\n'
            '        return _set()\n'
            '    found = {_id(value)}\n'
            '    for child in children:\n'
            '        found.update(containers(child))\n'
            '    return found\n'
            'sys.path.append(%r)\n'
            'import solution\n'
            'function = getattr(solution,payload["function"])\n'
            'observed = []\n'
            'for args in payload["cases"]:\n'
            '    returned = function(*args)\n'
            '    observed.append({"returned":encode(returned),"arguments":encode(args)})\n'
            '    if payload.get("output_copies", False):\n'
            '        observed[-1]["output_copies"] = not (containers(returned) & containers(args))\n'
            '_print(%r+_dumps(observed,allow_nan=False))\n'
        ) % (str(candidate),marker)
        execution = run_isolated(script,root,json.dumps({'function':function,'cases':cases,
                                                        'output_copies':check.get('output_copies') is True}))
        if execution is None:
            return result(False,'behavioral check timed out')
        code,stdout = execution
        if code != 0:
            return result(False,'behavioral computation failed')
        output = [line[len(marker):] for line in stdout.splitlines() if line.startswith(marker)]
        if len(output) != 1:
            return result(False,'missing behavioral results')
        actual = read_json(output[0])
        wanted = [{'returned':typed_value(value),'arguments':typed_value(args)}
                  for args,value in zip(cases,expected)]
        if check.get('output_copies') is True:
            for value in wanted:
                value['output_copies'] = True
        passed = same_value(actual,wanted)
        return result(passed,'trusted tests and behavioral cases passed' if passed else 'behavioral values or input mutation differ')


def run_code_tests(task,workspace,check):
    if check['kind'] == 'fix':
        originals = test_files(task/'workspace')
        candidates = test_files(workspace)
        if set(originals) != set(candidates):
            return result(False,'test files added or deleted')
        declared = check.get('tests')
        if not isinstance(declared,dict) or set(declared) != set(originals):
            return result(False,'invalid trusted test manifest')
        for name,path in originals.items():
            safe_relative(name)
            if path.is_symlink():
                return result(False,'trusted test symlink')
            content = path.read_bytes()
            if hashlib.sha256(content).hexdigest() != declared[name]:
                return result(False,'trusted test checksum mismatch')
            if content != candidates[name].read_bytes():
                return result(False,'test file changed: '+name)
    source = check.get('test_source')
    if not isinstance(source,str) or not source.strip():
        return result(False,'invalid hidden tests')
    with tempfile.TemporaryDirectory(prefix='crossfeed-check-') as temp:
        root = Path(temp)
        candidate = root/'candidate'
        shutil.copytree(workspace,candidate)
        trusted = root/'trusted'
        trusted.mkdir()
        (trusted/'test_hidden.py').write_text(source,encoding='utf-8')
        if check['kind'] == 'fix':
            # Run original tests, never candidate discovery, including nested tests.
            for index,path in enumerate(originals.values()):
                (trusted/('test_original_%d.py'%index)).write_bytes(path.read_bytes())
        marker = 'CROSSFEED_OK_'+uuid.uuid4().hex
        script = (
            'import sys, unittest\n'
            'sys.path.append(%r)\n'
            'suite = unittest.defaultTestLoader.discover(%r)\n'
            'outcome = unittest.TextTestRunner(verbosity=1).run(suite)\n'
            'if outcome.wasSuccessful() and outcome.testsRun:\n'
            '    print(%r)\n'
            '    sys.exit(0)\n'
            'sys.exit(1)\n'
        ) % (str(candidate),str(trusted),marker)
        execution = run_isolated(script,root)
        if execution is None:
            return result(False,'tests timed out')
        code,stdout = execution
        if code != 0 or marker not in stdout.splitlines():
            # Avoid reflecting arbitrary submitted output into the result.
            return result(False,'trusted tests failed (exit %d)' % code)
    # Parent assertions and expected values remain outside candidate interpreters.
    return check_behavior(workspace,check)


def json_answer(reply, kind):
    """Select final answer lines; extraction requires an unambiguous object."""
    if kind in ('repo-qa', 'reasoning', 'review'):
        trailing_content = False
        for line in reversed(reply.splitlines()):
            try:
                value = read_json(line)
            except ValueError:
                value = None
            if isinstance(value, dict):
                formatting = {'format_ok': not trailing_content}
                if trailing_content:
                    formatting['format_issue'] = 'fenced' if '```' in reply else 'extra prose'
                return value, formatting
            trailing_content = trailing_content or bool(line.strip())
        raise ValueError('missing JSON object line')
    decoder = json.JSONDecoder()
    candidates = []
    cursor = 0
    while cursor < len(reply):
        # Decode strings/arrays too so braces inside them are not candidates.
        if reply[cursor] not in '{["':
            cursor += 1
            continue
        try:
            value, end = decoder.raw_decode(reply, cursor)
        except ValueError:
            if reply[cursor] == '{':
                raise ValueError('malformed JSON object')
            cursor += 1
            continue
        if isinstance(value, dict):
            candidates.append(read_json(reply[cursor:end]))
        cursor = end
    if not candidates:
        raise ValueError('missing JSON object')
    if any(not same_value(value, candidates[0]) for value in candidates[1:]):
        raise ValueError('ambiguous JSON objects')
    fences = re.findall(r'^\s*```[^\n]*\n(.*?)^\s*```\s*$', reply, re.M | re.S)
    if '```' in reply and (len(fences) != 1 or len(candidates) != 1):
        raise ValueError('ambiguous fenced reply')
    try:
        strict = read_json(reply.strip() if kind == 'extraction' else reply.strip().splitlines()[-1])
        format_ok = same_value(strict, candidates[0]) and not fences and len(candidates) == 1
    except ValueError:
        format_ok = False
    formatting = {'format_ok': format_ok}
    if not format_ok:
        formatting['format_issue'] = 'fenced' if fences else 'extra prose'
    return candidates[0], formatting


def check_task(task_dir, workspace_dir, reply_file):
    verdict = _check_task(task_dir, workspace_dir, reply_file)
    verdict.setdefault('strict_pass', verdict['pass'])
    verdict.setdefault('format_ok', not verdict['reason'].startswith(('invalid input:', 'reply is empty')))
    return verdict


def _check_task(task_dir,workspace_dir,reply_file):
    try:
        task,workspace,reply_path = Path(task_dir),Path(workspace_dir),Path(reply_file)
        if reply_path.is_symlink() or (task/'check.json').is_symlink():
            return result(False,'symlink input rejected')
        from bench.run_grid import split_receipt
        reply, _ = split_receipt(reply_path.read_text(encoding='utf-8'))
        if not reply.strip():
            return result(False,'reply is empty')
        check = read_json((task/'check.json').read_text(encoding='utf-8'))
        if not isinstance(check,dict):
            return result(False,'check data must be an object')
        no_symlinks(workspace)
        kind = check.get('kind')
        if kind == 'history-fix':
            from bench.history import check_history
            return check_history(task, workspace, check)
        if kind in ('fix','implement'):
            return run_code_tests(task,workspace,check)
        actual, formatting = json_answer(reply, kind)
        def judged(passed, reason):
            return dict(result(passed, reason), strict_pass=bool(passed and formatting['format_ok']), **formatting)
        if kind == 'extraction':
            expected = check['expected']
            if not isinstance(actual,dict) or not isinstance(expected,dict):
                return judged(False,'extraction requires a JSON object')
            passed = all(key in actual and same_value(actual[key],value) for key,value in expected.items())
            return judged(passed,'checked fields match' if passed else 'checked fields differ')
        if not isinstance(actual,dict):
            return judged(False,'final JSON line must be an object')
        if kind == 'review':
            file = actual.get('bug_file')
            if isinstance(file,str) and file.startswith('./'):
                file = file[2:]
            safe_relative(file)
            line = actual.get('bug_line')
            passed = file == check['bug_file'] and type(line) is int and abs(line-check['bug_line']) <= 1
            return judged(passed,'bug location matches' if passed else 'bug location differs')
        if kind in ('repo-qa','reasoning'):
            expected = check['answer']
            passed = 'answer' in actual and same_value(normalize(actual['answer']),normalize(expected))
            return judged(passed,'answer matches' if passed else 'answer differs')
        return judged(False,'unknown check kind')
    except (OSError,ValueError,TypeError,KeyError,UnicodeError,RecursionError) as exc:
        return result(False,'invalid input: '+type(exc).__name__)
    except Exception as exc:
        return result(False,'check error: '+type(exc).__name__)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    output = (check_task(*args) if len(args) == 3 else
              result(False,'usage: check.py TASK_DIR WORKSPACE_DIR REPLY_FILE'))
    output.setdefault('strict_pass', output['pass'])
    output.setdefault('format_ok', False)
    print(json.dumps(output,sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
