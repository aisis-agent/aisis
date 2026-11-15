#!/usr/bin/env python3
"""Native-CI fixture, without daemon or device operations."""
import json
from pathlib import Path
import sys
with (Path(__file__).parent / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['docker', *sys.argv[1:]]) + '\n')
