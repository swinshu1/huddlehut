from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import guard_marker  # noqa: F401


NAME = "muse_guard_python_fixture"
VERSION = "1.0.0"
DIST_INFO = f"{NAME}-{VERSION}.dist-info"


def _write_metadata(parent):
    dist_info = Path(parent, DIST_INFO)
    dist_info.mkdir()
    Path(dist_info, "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n",
        encoding="utf-8",
    )
    Path(dist_info, "WHEEL").write_text(
        "Wheel-Version: 1.0\nGenerator: muse-guard-fixture\n"
        "Root-Is-Purelib: true\nTag: py3-none-any\n",
        encoding="utf-8",
    )
    return DIST_INFO


def get_requires_for_build_wheel(config_settings=None):
    return []


def get_requires_for_build_editable(config_settings=None):
    return []


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _write_metadata(metadata_directory)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _write_metadata(metadata_directory)


def _build_wheel(wheel_directory):
    filename = f"{NAME}-{VERSION}-py3-none-any.whl"
    wheel_path = Path(wheel_directory, filename)
    with ZipFile(wheel_path, "w", ZIP_DEFLATED) as wheel:
        wheel.writestr(f"{DIST_INFO}/METADATA", f"Name: {NAME}\nVersion: {VERSION}\n")
        wheel.writestr(
            f"{DIST_INFO}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(f"{DIST_INFO}/RECORD", "")
    return filename


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_wheel(wheel_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build_wheel(wheel_directory)
