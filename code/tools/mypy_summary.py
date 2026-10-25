"""Summarise a mypy run by file and error code.

    python code/tools/mypy_summary.py mypy_output.txt
"""

import collections
import re
import sys

pattern = re.compile(r"^(\S+?\.py):\d+: error: .*\[([\w-]+)\]\s*$")
by_file: collections.Counter[str] = collections.Counter()
by_code: collections.Counter[str] = collections.Counter()
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    m = pattern.match(line.strip())
    if m:
        by_file[m.group(1)] += 1
        by_code[m.group(2)] += 1
print("total", sum(by_file.values()))
for name, n in by_file.most_common():
    print(f"{n:4d} {name}")
print()
for name, n in by_code.most_common():
    print(f"{n:4d} {name}")
