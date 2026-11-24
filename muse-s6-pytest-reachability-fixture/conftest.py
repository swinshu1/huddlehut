import hashlib
import os
import stat
import sys


sys.dont_write_bytecode = True


def _probe(label, path):
    readable = os.access(path, os.R_OK)
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        exists = False
        regular = False
    else:
        exists = True
        regular = stat.S_ISREG(mode)

    return (
        f"{label}:exists={str(exists).lower()},"
        f"readable={str(readable).lower()},regular={str(regular).lower()}"
    )


canary_path = "/home/hatch/workspace/your_files/S6 Reach 9067.md"
print(
    "S6_PYTEST_REACH_CTRL_9067 "
    + _probe("CANARY", canary_path)
)


def pytest_generate_tests(metafunc):
    if "case_name" not in metafunc.fixturenames:
        return

    cases = ("alpha", "beta", "gamma")
    ids = [f"{case}-{hashlib.sha256(case.encode()).hexdigest()[:10]}" for case in cases]
    metafunc.parametrize("case_name", cases, ids=ids)
