"""Local operations anchored to a project directory without following symlinks."""

import os
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from secrets import token_hex
from typing import BinaryIO


class LocalError(ValueError):
    pass


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@contextmanager
def project_parent(
    root: Path, path: Path, *, create_zed: bool = False, workspace_identity: tuple[int, int] | None = None,
) -> Iterator[int]:
    parts = path.relative_to(root).parts
    if not parts or ".." in parts:
        raise LocalError("Percorso locale fuori dal progetto.")

    directory = os.open(root, _directory_flags())
    try:
        if workspace_identity is not None:
            info = os.fstat(directory)
            if (info.st_dev, info.st_ino) != workspace_identity:
                raise LocalError("La directory del progetto è cambiata. Riapri il progetto e verifica la configurazione.")
        if create_zed:
            # This operation is only used to install the packaged .zed/tasks.json.
            try:
                os.mkdir(".zed", mode=0o755, dir_fd=directory)
            except FileExistsError:
                pass
        for part in parts[:-1]:
            child = os.open(part, _directory_flags(), dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def snapshot_upload(
    directory: int, name: str, destination: Path, *, expected_source: os.stat_result | None = None,
) -> None:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd, "rb") as source:
        original = os.fstat(source.fileno())
        if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
            raise LocalError("Il file da caricare deve essere regolare e privo di hard link.")
        if expected_source is not None and _identity(original) != _identity(expected_source):
            raise LocalError("Il file è cambiato durante la conferma. Salvalo di nuovo per caricarlo.")
        with destination.open("xb") as staged:
            shutil.copyfileobj(source, staged)
            os.fchmod(staged.fileno(), stat.S_IMODE(original.st_mode) & 0o755)
        if _identity(original) != _identity(os.fstat(source.fileno())):
            raise LocalError("Il file locale è cambiato durante la lettura. Riprova.")


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def existing_file(directory: int, name: str) -> os.stat_result | None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise LocalError("Il percorso locale deve puntare a un file regolare senza symlink.")
    return info


def install_file(
    directory: int, name: str, source: BinaryIO, original: os.stat_result | None,
) -> None:
    temp_name = f".zed-transfer-{token_hex(12)}.part"
    fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as staged:
            shutil.copyfileobj(source, staged)
            if original is not None:
                os.fchmod(staged.fileno(), stat.S_IMODE(original.st_mode) & 0o777)
            staged.flush()
            os.fsync(staged.fileno())

        if original is None:
            try:
                # Publish atomically and fail if another process created this name.
                os.link(temp_name, name, src_dir_fd=directory, dst_dir_fd=directory)
            except FileExistsError as error:
                raise LocalError("Il file di destinazione esiste già. Riprova.") from error
        else:
            current = existing_file(directory, name)
            if current is None or _identity(current) != _identity(original):
                raise LocalError("Il file locale è cambiato durante il download. Riprova.")
            os.replace(temp_name, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory)
        except FileNotFoundError:
            pass
