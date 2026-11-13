"""Do not publish a release until every platform installer is present."""
import os
from pathlib import Path
import shlex
import subprocess
import textwrap
import re

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='Release publication runs in Bash on Linux')
@pytest.mark.parametrize('missing', [None, 'macOS-arm64.dmg', 'macOS-arm64.zip',
                                     'windows-x64.zip', 'windows-x64-setup.exe', 'linux-amd64.deb'])
def test_release_publish_requires_all_five_installers(missing):
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
    publish = workflow.split('      - name: Verify all installers and publish\n', 1)[1]
    script = textwrap.dedent(publish.split('        run: |\n', 1)[1])
    suffixes = ['macOS-arm64.dmg', 'macOS-arm64.zip', 'windows-x64.zip',
                'windows-x64-setup.exe', 'linux-amd64.deb']
    assets = '\n'.join('Stellar-Smart-Terminal-v1.2.3-' + suffix
                       for suffix in suffixes if suffix != missing)
    # Replace only the external GitHub operation; run the actual publish script.
    mock_gh = f'''gh() {{
  case "$1 $2" in
    'release view') printf '%s\\n' {shlex.quote(assets)} ;;
    'release edit') echo PUBLISHED ;;
    *) return 1 ;;
  esac
}}
GITHUB_REF_NAME=v1.2.3
'''
    # The runner's working directory is disposable; keep assets.txt out of the repo.
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(['/bin/bash', '-e'], input=mock_gh + script, text=True,
                                cwd=directory, capture_output=True, timeout=5)
    if missing is None:
        assert result.returncode == 0, result.stderr
        assert 'PUBLISHED' in result.stdout
    else:
        assert result.returncode != 0
        assert missing in result.stderr
        assert 'PUBLISHED' not in result.stdout


@pytest.mark.skipif(os.name == 'nt', reason='Release uploads run in Bash')
@pytest.mark.parametrize('platform', [0, 1, 2])
@pytest.mark.parametrize('draft', [True, False])
def test_uploads_leave_public_installers_unchanged(platform, draft):
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/release.yml').read_text()
    upload = workflow.split('      - name: Upload to release\n')[platform + 1]
    body = re.search(r'(?m)^        run: \|\n((?:          .*\n|\n)*)', upload)[1]
    script = textwrap.dedent(body)
    mock = f'''gh() {{
  case "$1 $2" in
    'release view') printf '%s\\n' {str(draft).lower()} ;;
    'release upload') echo UPLOADED ;;
    *) return 1 ;;
  esac
}}
GITHUB_REF_NAME=v1.2.3
'''
    result = subprocess.run(['/bin/bash', '-e'], input=mock + script, text=True,
                            capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert ('UPLOADED' in result.stdout) is draft
