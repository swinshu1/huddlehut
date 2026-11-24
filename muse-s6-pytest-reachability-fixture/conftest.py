import hashlib
import sys


sys.dont_write_bytecode = True
print("S6_PYTEST_REACH_CTRL_9067")


def pytest_generate_tests(metafunc):
    if "case_name" not in metafunc.fixturenames:
        return

    cases = ("alpha", "beta", "gamma")
    ids = [f"{case}-{hashlib.sha256(case.encode()).hexdigest()[:10]}" for case in cases]
    metafunc.parametrize("case_name", cases, ids=ids)
