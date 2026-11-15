"""Native-CI fixture for deployment ordering, not corpus acceptance."""
import json
from pathlib import Path
import sys
with (Path(__file__).parent / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['corpus', *sys.argv[1:]]) + '\n')
print('fixture corpus stdout is retained')
