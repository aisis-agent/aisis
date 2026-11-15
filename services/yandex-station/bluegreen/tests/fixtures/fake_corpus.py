"""Native-CI fixture for deployment ordering, not corpus acceptance."""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['corpus', *sys.argv[1:]]) + '\n')
if os.environ.get('CHANGE_ACTIVE_AFTER_STAGE') == '1' and 'aisis-quasar-stage-green' in sys.argv:
    state = json.loads((root / 'state.json').read_text())
    state['active'] = state['idle_color']
    (root / 'state.json').write_text(json.dumps(state))
print('fixture corpus stdout is retained')
