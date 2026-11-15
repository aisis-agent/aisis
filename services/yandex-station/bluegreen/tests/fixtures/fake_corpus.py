"""Native-CI fixture for deployment ordering, not corpus acceptance."""
import json
import os
from pathlib import Path
import sys

root = Path(__file__).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['corpus', *sys.argv[1:]]) + '\n')
state = json.loads((root / 'state.json').read_text())
if os.environ.get('CHANGE_ACTIVE_AFTER_STAGE') == '1' and 'aisis-quasar-stage-green' in sys.argv:
    state['active'] = state['idle_color']
loss = os.environ.get('LEASE_LOSS_DURING_IDLE', '')
if loss and 'aisis-quasar-prod-' + state['idle_color'] in sys.argv:
    marker = Path(os.environ['AISIS_BG_STATE']) / 'maintenance'
    lease = json.loads(marker.read_text())
    if loss == 'owner':
        lease['owner'] = 'f' * 32
    else:
        lease['deadline_monotonic'] = 0
    marker.write_text(json.dumps(lease))
    if loss == 'active':
        state['active'] = state['idle_color']
    if loss == 'container':
        state['actual_id'] = 'another-owners-container'
        state['actual'] = {'image': 'another-owners-image', 'artifact': ''}
(root / 'state.json').write_text(json.dumps(state))
print('fixture corpus stdout is retained')
