#!/usr/bin/env python3
"""
Bump the ?v= cache key on app.js and style.css.

GitHub Pages serves assets with a 10-minute cache, and index.html can refresh
before app.js does. That shipped a real broken state to a returning visitor: new
markup driven by a script that had never heard of it — the loading overlay stayed
up forever because the cached script had no code to hide it.

Run before pushing any change to app.js or style.css, or let `make deploy` do it.

Usage:  python3 build/bump_version.py
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(ROOT, "index.html")

PAT = re.compile(r'(href|src)="(app\.js|style\.css)\?v=(\d+)"')


def main():
    html = open(INDEX).read()
    found = PAT.findall(html)
    if not found:
        print("no ?v= markers found in index.html — did the tags change?", file=sys.stderr)
        return 1

    nxt = max(int(v) for _, _, v in found) + 1
    html = PAT.sub(lambda m: f'{m.group(1)}="{m.group(2)}?v={nxt}"', html)
    open(INDEX, "w").write(html)
    print(f"cache key -> v={nxt} ({len(found)} refs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
