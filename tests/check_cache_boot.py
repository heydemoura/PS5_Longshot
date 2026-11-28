"""Runs one check as `python -I -B <check> <arguments>` would, with its compiler calls through the compile cache
(tests/check_cache.py). tests/run_all.py starts each check this way unless LONGSHOT_CHECK_CACHE=0.

    python -I -B tests/check_cache_boot.py <check.py> [arguments...]
"""
import os
import runpy
import sys

# (-I puts no script's folder on sys.path: not this one, nor the check's, so the check sees the path it would have)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import check_cache
finally:
    sys.path.pop(0)

check = os.path.abspath(sys.argv[1])
sys.argv = [check] + sys.argv[2:]
check_cache.install()
runpy.run_path(check, run_name="__main__")
