from __future__ import annotations

import hashlib
import logging

import pytest


_PAYLOAD = (
    "import importlib.util as _importlib_util\n"
    "import pathlib as _pathlib\n"
    "import sys as _sys\n"
    "import sysconfig as _sysconfig\n"
    "\n"
    "print(\"S7B_CWD_SHADOW_CTRL_7318\", file=_sys.stderr)\n"
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
