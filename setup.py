"""Setuptools hooks for deterministic Tasktra package resources."""

from __future__ import annotations

import gzip
import os
from pathlib import Path
import shutil
import tarfile

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist


class build_py(_build_py):
    """Place the canonical catalog beside the runtime package in each wheel."""

    def run(self) -> None:
        super().run()
        source = Path(__file__).parent / "catalog"
        destination = Path(self.build_lib) / "tasktra" / "catalog"
        shutil.copytree(source, destination, dirs_exist_ok=True)


class sdist(_sdist):
    """Normalize archive metadata when the standard reproducibility epoch is set."""

    def make_distribution(self) -> None:
        super().make_distribution()
        raw_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        if raw_epoch is None:
            return
        try:
            epoch = int(raw_epoch)
        except ValueError as error:
            raise ValueError("SOURCE_DATE_EPOCH must be an integer") from error
        for archive in self.archive_files:
            path = Path(archive)
            if path.suffixes[-2:] == [".tar", ".gz"]:
                self._normalize_gztar(path, epoch)

    @staticmethod
    def _normalize_gztar(path: Path, epoch: int) -> None:
        entries: list[tuple[tarfile.TarInfo, bytes | None]] = []
        with tarfile.open(path, "r:gz") as source:
            for member in source.getmembers():
                content = source.extractfile(member).read() if member.isfile() else None
                member.mtime = epoch
                member.uid = member.gid = 0
                member.uname = member.gname = ""
                entries.append((member, content))
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as raw, gzip.GzipFile(
            filename="", mode="wb", fileobj=raw, mtime=epoch
        ) as compressed, tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
            for member, content in entries:
                target.addfile(member, fileobj=None if content is None else __import__("io").BytesIO(content))
        temporary.replace(path)


setup(cmdclass={"build_py": build_py, "sdist": sdist})
