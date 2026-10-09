"""Verified, offline historical clients for real migration regression fixtures."""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
from zipfile import ZipFile


FIXTURES = Path(__file__).parent / "fixtures"


def extract_client(name: str, destination: Path) -> Path:
    archive = FIXTURES / f"tasktra-{name}-client.zip"
    manifest = json.loads(archive.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    with ZipFile(archive) as reader:
        expected = manifest["files"]
        assert reader.namelist() == [entry["path"] for entry in expected]
        for entry in expected:
            relative = PurePosixPath(entry["path"])
            assert not relative.is_absolute() and ".." not in relative.parts and "\\" not in entry["path"]
            contents = reader.read(entry["path"])
            assert sha256(contents).hexdigest() == entry["sha256"]
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
    return destination


def run_client(source: Path, program: str, *arguments: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    result = subprocess.run([sys.executable, "-c", program, *arguments], env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise AssertionError(result.stderr)
    return result
