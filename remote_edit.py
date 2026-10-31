"""Remote edit conflict checks and POSIX atomic-upload command construction."""
import errno
import hashlib
import re
import stat

from i18n import t


class RemoteFileConflictError(RuntimeError):
    def __init__(self):
        super().__init__(t('remote.sync_conflict'))


def sftp_target(sftp, path):
    """Follow a file symlink, retaining its target's permission bits."""
    try:
        attr = sftp.lstat(path)
    except OSError as exc:
        if exc.errno != errno.ENOENT:
            raise
        return path, None
    if stat.S_ISLNK(attr.st_mode):
        path = sftp.normalize(path)
        attr = sftp.stat(path)
    if not stat.S_ISREG(attr.st_mode):
        raise OSError('Upload destination is not a regular file')
    return path, stat.S_IMODE(attr.st_mode)


def sftp_digest(sftp, path):
    digest = hashlib.sha256()
    with sftp.open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(256 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def check_remote_digest(actual, expected, desired):
    # A lost connection after rename may leave a completed upload unacknowledged.
    if actual != expected and actual != desired:
        raise RemoteFileConflictError()


def atomic_upload_command(quoted_path, quoted_temporary, expected=None, desired=None):
    """One POSIX shell command; temporary creation is exclusive and owned.

    Resolve file symlinks before publishing. stat supports GNU and BSD hosts.
    Optional digest checks happen after transfer, immediately before rename.
    """
    if expected is not None and not all(
            isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)
            for value in (expected, desired)):
        raise ValueError('Invalid remote edit digest')
    command = (
        f'target={quoted_path}; n=0; '
        'while [ -L "$target" ]; do '
        'n=$((n+1)); [ "$n" -le 40 ] || exit 1; '
        'link=$(readlink "$target") || exit 1; '
        'case "$link" in /*) target=$link;; '
        '*) target=$(dirname "$target")/$link;; esac; done; '
        f'temporary={quoted_temporary}; '
        'temporary=$(dirname "$target")/$(basename "$temporary"); '
        'set -C; exec 3> "$temporary" || exit 1; '
        "trap 'rm -f -- \"$temporary\"' 0; "
        'cat >&3 || exit 1; '
        'if [ -e "$target" ]; then [ -f "$target" ] || exit 1; '
        'mode=$(stat -c %a -- "$target" 2>/dev/null || stat -f %Lp "$target") || exit 1; '
        'chmod "$mode" "$temporary" || exit 1; fi; '
    )
    if expected is not None:
        command += (
            'hash_file() { '
            'if command -v sha256sum >/dev/null 2>&1; then sha256sum < "$1"; '
            'elif command -v shasum >/dev/null 2>&1; then shasum -a 256 < "$1"; '
            'else openssl dgst -sha256 < "$1"; fi; }; '
            'actual=$(hash_file "$target") || { echo STELLAR_EDIT_CONFLICT >&2; exit 1; }; '
            # GNU/shasum output starts with hash; OpenSSL output ends with hash.
            f'case " $actual " in *" {expected} "*|*" {desired} "*) ;; '
            '*) echo STELLAR_EDIT_CONFLICT >&2; exit 1;; esac; '
        )
    return command + 'mv -- "$temporary" "$target"'
