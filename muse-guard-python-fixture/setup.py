import sys

import guard_marker  # noqa: F401


if sys.argv[1:] != ["--version"]:
    raise SystemExit("this fixture supports only setup.py --version")

print("1.0.0")
