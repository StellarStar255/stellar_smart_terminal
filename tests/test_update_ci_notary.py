"""Credentials must be verified before upload and never appear in helper output."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def helper():
    spec = importlib.util.spec_from_file_location(
        'update_ci_notary', Path(__file__).resolve().parents[1] / 'scripts/update_ci_notary.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_credentials_do_not_update_secrets_or_leak_password(capsys):
    module = helper()
    password = 'fake-app-password'
    results = [SimpleNamespace(returncode=0, stdout=password + '\n'),
               SimpleNamespace(returncode=1, stderr='HTTP status code: 401 ' + password)]
    with patch.object(module.sys, 'platform', 'darwin'), \
         patch.object(module.sys, 'argv', ['helper', '--apple-id', 'test@example.invalid']), \
         patch.object(module.subprocess, 'run', side_effect=results) as run:
        assert module.main() == 1
    assert run.call_count == 2
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert '401' in output.out


def test_valid_credentials_update_only_the_two_notary_secrets(capsys):
    module = helper()
    password = 'fake-app-password'
    results = [SimpleNamespace(returncode=0, stdout=password + '\n')]
    results += [SimpleNamespace(returncode=0)] * 3
    with patch.object(module.sys, 'platform', 'darwin'), \
         patch.object(module.sys, 'argv', ['helper', '--apple-id', 'test@example.invalid']), \
         patch.object(module.subprocess, 'run', side_effect=results) as run:
        assert module.main() == 0
    calls = run.call_args_list[2:]
    assert [c.args[0][3] for c in calls] == ['NOTARY_APPLE_ID', 'NOTARY_PASSWORD']
    assert all(c.args[0][-1] == module.REPO for c in calls)
    assert calls[1].kwargs['input'] == password
    assert all(password not in ' '.join(c.args[0]) for c in calls)
    output = capsys.readouterr()
    assert password not in output.out + output.err


def test_cancel_changes_nothing(capsys):
    module = helper()
    with patch.object(module.sys, 'platform', 'darwin'), \
         patch.object(module.sys, 'argv', ['helper', '--apple-id', 'test@example.invalid']), \
         patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as run:
        assert module.main() == 1
    assert run.call_count == 1
