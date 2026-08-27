"""Shared test configuration.

Mirrors what `scrapev3.cli._configure_event_loop` does at runtime: on Windows,
asyncio's default ProactorEventLoop lacks `add_reader`/`add_writer`, which
curl_cffi needs. Without this the tests still pass, but curl_cffi quietly
spawns an extra selector thread per loop and warns about it.
"""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
