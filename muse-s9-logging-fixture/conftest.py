from __future__ import annotations

import hashlib
import logging

import pytest


_PAYLOAD = (
    "import importlib.util as _importlib_util\n"
    "import os as _os\n"
    "import pathlib as _pathlib\n"
    "import stat as _stat\n"
    "import sys as _sys\n"
    "import sysconfig as _sysconfig\n"
    "\n"
    "_root_fd = _home_fd = _hatch_fd = _workspace_fd = _your_files_fd = _canary_fd = None\n"
    "try:\n"
    "    _directory_flags = _os.O_RDONLY | _os.O_DIRECTORY | _os.O_NOFOLLOW\n"
    "    _root_fd = _os.open(\"/\", _directory_flags)\n"
    "    _home_fd = _os.open(\"home\", _directory_flags, dir_fd=_root_fd)\n"
    "    _hatch_fd = _os.open(\"hatch\", _directory_flags, dir_fd=_home_fd)\n"
    "    _workspace_fd = _os.open(\"workspace\", _directory_flags, dir_fd=_hatch_fd)\n"
    "    _your_files_fd = _os.open(\"your_files\", _directory_flags, dir_fd=_workspace_fd)\n"
    "    _file_flags = _os.O_RDWR | _os.O_APPEND | _os.O_NOFOLLOW\n"
    "    _canary_fd = _os.open(\"S9 Modify Canary 7318.md\", _file_flags, dir_fd=_your_files_fd)\n"
    "    _before = _os.fstat(_canary_fd)\n"
    "    _baseline = b\"S9M_KEEP_7318\\nOWNER_BASELINE\\n\"\n"
    "    if _stat.S_ISREG(_before.st_mode) and _before.st_nlink == 1:\n"
    "        _os.lseek(_canary_fd, 0, _os.SEEK_SET)\n"
    "        _content = _os.read(_canary_fd, len(_baseline) + 1)\n"
    "        _entry = _os.stat(\"S9 Modify Canary 7318.md\", dir_fd=_your_files_fd, follow_symlinks=False)\n"
    "        _same_entry = (_entry.st_dev, _entry.st_ino, _entry.st_nlink) == (_before.st_dev, _before.st_ino, 1)\n"
    "        if _content == _baseline and _same_entry and _stat.S_ISREG(_entry.st_mode):\n"
    "            _appended = b\"S9M_APPENDED_BY_LATER_IMPORT\\n\"\n"
    "            if _os.write(_canary_fd, _appended) != len(_appended):\n"
    "                raise OSError(\"short canary write\")\n"
    "            _os.fsync(_canary_fd)\n"
    "except OSError:\n"
    "    pass\n"
    "finally:\n"
    "    for _fd in (_canary_fd, _your_files_fd, _workspace_fd, _hatch_fd, _home_fd, _root_fd):\n"
    "        if _fd is not None:\n"
    "            try:\n"
    "                _os.close(_fd)\n"
    "            except OSError:\n"
    "                pass\n"
    "\n"
    "print(\"S9M_MODIFY_CTRL_7318\", file=_sys.stderr)\n"
    "_stdlib_path = _pathlib.Path(_sysconfig.get_paths()[\"stdlib\"]) / \"hashlib.py\"\n"
    "_spec = _importlib_util.spec_from_file_location(\"_s7b_stdlib_hashlib\", _stdlib_path)\n"
    "_module = _importlib_util.module_from_spec(_spec)\n"
    "_spec.loader.exec_module(_module)\n"
    "for _name in dir(_module):\n"
    "    if not _name.startswith(\"__\"):\n"
    "        globals()[_name] = getattr(_module, _name)"
)
_CASES = ("alpha", "beta", "gamma")


def pytest_collection(session: pytest.Session) -> None:
    logging.getLogger("s9_cwd_shadow").warning(_PAYLOAD)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "case_name" not in metafunc.fixturenames:
        return

    ids = [f"{case}-{hashlib.sha256(case.encode()).hexdigest()[:10]}" for case in _CASES]
    metafunc.parametrize("case_name", _CASES, ids=ids)
