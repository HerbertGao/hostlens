"""Unit tests for the shared ``extract_single_regular_file`` tar helper.

These exercise ``hostlens.targets._tar`` directly with constructed tar byte
streams (stdlib ``tarfile`` + ``io.BytesIO``), independent of any container
SDK. ``not_a_file`` / ``file_too_large`` are injected as factory lambdas that
return identifiable ``TargetError`` instances, mirroring how ``DockerTarget``
and ``KubernetesTarget`` attach their own ``target`` / ``path`` context.
"""

from __future__ import annotations

import io
import tarfile

import pytest

from hostlens.core.exceptions import TargetError
from hostlens.targets._tar import READ_FILE_MAX_BYTES, extract_single_regular_file


def _not_a_file() -> TargetError:
    return TargetError(kind="not_a_file", target="t", path="/p")


def _file_too_large(size: int) -> TargetError:
    return TargetError(kind="file_too_large", target="t", path="/p", size=size)


def _extract(tar_bytes: bytes) -> bytes:
    return extract_single_regular_file(
        io.BytesIO(tar_bytes),
        not_a_file=_not_a_file,
        file_too_large=_file_too_large,
    )


def _tar_regular(name: str, data: bytes) -> bytes:
    """Build a tar archive with a single regular file ``name``/``data``."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.type = tarfile.REGTYPE
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_dir_first(dir_name: str, member: str, data: bytes) -> bytes:
    """Build a tar archive whose first entry is a directory, then a file."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dir_info = tarfile.TarInfo(name=dir_name)
        dir_info.type = tarfile.DIRTYPE
        tar.addfile(dir_info)
        file_info = tarfile.TarInfo(name=member)
        file_info.size = len(data)
        file_info.type = tarfile.REGTYPE
        tar.addfile(file_info, io.BytesIO(data))
    return buf.getvalue()


def _tar_two_files(name_a: str, name_b: str, data: bytes) -> bytes:
    """Build a tar archive with two regular files."""

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name in (name_a, name_b):
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_single_regular_file_returns_bytes() -> None:
    data = _extract(_tar_regular("tmp/hello.txt", b"hello world"))
    assert data == b"hello world"


def test_exactly_cap_allowed() -> None:
    payload = b"a" * READ_FILE_MAX_BYTES
    data = _extract(_tar_regular("tmp/exact.bin", payload))
    assert len(data) == READ_FILE_MAX_BYTES


def test_over_cap_raises_file_too_large() -> None:
    payload = b"a" * (READ_FILE_MAX_BYTES + 1)
    with pytest.raises(TargetError) as e:
        _extract(_tar_regular("tmp/big.bin", payload))
    assert e.value.kind == "file_too_large"


def test_directory_first_entry_not_a_file() -> None:
    with pytest.raises(TargetError) as e:
        _extract(_tar_dir_first("etc/", "etc/passwd", b"root:x:0:0"))
    assert e.value.kind == "not_a_file"


def test_two_regular_files_not_a_file() -> None:
    with pytest.raises(TargetError) as e:
        _extract(_tar_two_files("a.txt", "b.txt", b"x"))
    assert e.value.kind == "not_a_file"


def test_empty_or_truncated_stream_not_a_file() -> None:
    with pytest.raises(TargetError) as e:
        _extract(b"")
    assert e.value.kind == "not_a_file"


def test_extractfile_none_returns_empty_bytes() -> None:
    """A regular member whose ``extractfile`` yields ``None`` → empty bytes.

    A zero-size regular file is the natural way to drive this: ``tarfile``
    returns a reader that immediately yields no bytes, and the helper maps a
    missing reader / empty content to ``b""`` without raising.
    """

    data = _extract(_tar_regular("tmp/empty.txt", b""))
    assert data == b""
