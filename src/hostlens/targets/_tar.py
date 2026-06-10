"""Shared single-regular-file tar extraction for ``read_file`` over a tar stream.

Both ``DockerTarget.read_file`` (``get_archive`` daemon-side tar) and
``KubernetesTarget.read_file`` (``tar cf -`` over exec stdout) read a path
back as a tar stream and must apply identical semantics:

- exactly **one regular file** in the archive;
- the first non-regular-file entry (directory / symlink / FIFO / device) →
  ``not_a_file`` — decided **before** ``file_too_large`` so a directory
  archive whose member exceeds the cap still reports ``not_a_file``;
- a second regular-file entry → ``not_a_file``;
- the single regular file ``> 10 MiB`` → ``file_too_large`` (boundary is
  strict ``>``; exactly 10 MiB is allowed). The size cap uses an
  unconditional running-byte backstop while reading the member, not just
  the tar-header ``size`` (which a hostile / busybox tar could understate).

Sharing this single forward pass keeps the two targets from drifting. The
``tarfile`` open mode is ``r|*`` (streaming, forward-only), so the fileobj
need only implement ``read`` forward — no seek/tell.
"""

from __future__ import annotations

import tarfile
from typing import TYPE_CHECKING, Any, Final, Protocol

from hostlens.core.exceptions import TargetError

if TYPE_CHECKING:
    from collections.abc import Callable


__all__ = ["READ_FILE_MAX_BYTES", "extract_single_regular_file"]


# 10 MiB cap for ``read_file`` (mirrors LocalTarget / SSHTarget / DockerTarget;
# boundary is strict ``>`` — exactly 10 MiB is allowed through).
READ_FILE_MAX_BYTES: Final[int] = 10 * 1024 * 1024

# Chunk size for the running-byte backstop while streaming a tar member.
_READ_CHUNK_BYTES: Final[int] = 64 * 1024


class _TarFileobj(Protocol):
    """Minimal sequential byte-source the streaming tar reader consumes.

    ``tarfile.open(mode="r|*")`` only ever calls ``read`` forward, so this
    pins exactly that surface for ``mypy --strict``.
    """

    def read(self, size: int = -1, /) -> bytes: ...


def extract_single_regular_file(
    fileobj: _TarFileobj,
    *,
    not_a_file: Callable[[], TargetError],
    file_too_large: Callable[[int], TargetError],
) -> bytes:
    """Single forward pass over a tar stream → the one regular file's bytes.

    ``not_a_file`` / ``file_too_large`` are factories so each caller can
    attach its own ``target`` / ``path`` context to the raised
    ``TargetError``. ``not_a_file`` is decided before ``file_too_large``
    (the first non-regular-file entry, or a second regular file, raises
    immediately). Raises the caller's ``not_a_file`` when the archive has
    no regular file at all (directory-only / empty archive).
    """

    try:
        with tarfile.open(fileobj=fileobj, mode="r|*") as tar:  # type: ignore[call-overload]
            data: bytes | None = None
            for member in tar:
                if not member.isreg():
                    raise not_a_file()
                if data is not None:
                    # Already saw a regular file; a second one means the
                    # path was a directory (multi-entry archive).
                    raise not_a_file()
                if member.size > READ_FILE_MAX_BYTES:
                    raise file_too_large(member.size)
                extracted = tar.extractfile(member)
                data = b"" if extracted is None else _read_capped(extracted, file_too_large)
            if data is None:
                raise not_a_file()
            return data
    except tarfile.ReadError as exc:
        # An empty / truncated / non-tar byte stream (e.g. the exec channel
        # dropped before any archive bytes, or a malformed header) is not a
        # readable regular file — surface ``not_a_file`` rather than letting
        # the raw ``ReadError`` escape.
        raise not_a_file() from exc


def _read_capped(reader: Any, file_too_large: Callable[[int], TargetError]) -> bytes:
    """Stream ``reader`` accumulating bytes; abort if total exceeds 10 MiB.

    Unconditional backstop: never read the whole stream into memory before
    checking size — accumulate chunk by chunk and raise the caller's
    ``file_too_large`` the moment the running total exceeds the cap.
    """

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > READ_FILE_MAX_BYTES:
            raise file_too_large(total)
        chunks.append(chunk)
    return b"".join(chunks)
