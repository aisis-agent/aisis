"""Native-CI fixture: desired and actual container state are independent."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).parent
ARGS = sys.argv[1:]
with (ROOT / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['bg', *ARGS]) + '\n')
STATE = json.loads((ROOT / 'state.json').read_text())


def get_value():
    key = ARGS[2]
    if key == 'active':
        return STATE['active']
    if key.startswith('IMG_'):
        return STATE['images'][key[4:].lower()]
    if key.startswith('ARTIFACT_'):
        return STATE['artifacts'][key[9:].lower()]
    return ''


def recreate():
    STATE['recreate_attempts'] += 1
    desired = {'image': STATE['images']['blue'], 'artifact': STATE['artifacts']['blue']}
    if os.environ.get('FAIL_RESTORE') == '1' and desired['image'] == 'previous-blue':
        print('fixture: previous idle recreation failed', file=sys.stderr)
        return 1
    STATE['actual'] = desired
    if os.environ.get('FAIL_FIRST_RECREATE') == '1' and STATE['recreate_attempts'] == 1:
        print('fixture: candidate recreation failed after replacement', file=sys.stderr)
        return 1
    return 0


def dispatch():
    command = ARGS[0]
    if command == 'get':
        print(get_value())
    elif command == 'import-artifact':
        print('/installed/new-artifact')
    elif command == 'artifact-image':
        print('sha256:' + 64 * '1')
    elif command == 'set-image' and ARGS[1] == 'prod':
        STATE['images'][ARGS[2]] = ARGS[3]
        STATE['artifacts'][ARGS[2]] = ARGS[5] if len(ARGS) > 5 else ''
    elif ARGS[:3] == ['compose', 'prod', 'up']:
        return recreate()
    elif command == 'verify-runtime' and ARGS[1] == 'prod':
        expected = {'image': STATE['images']['blue'], 'artifact': STATE['artifacts']['blue']}
        return int(STATE['actual'] != expected)
    elif command == 'container-id':
        print('fixture-idle-container')
    elif command == 'remove-inactive':
        STATE['actual'] = None
    elif command == 'flip':
        STATE['active'] = ARGS[2]
    elif ARGS == ['smoke', 'prod', 'blue']:
        return int(os.environ.get('FAIL_IDLE') == '1' and STATE['actual']['image'] != 'previous-blue')
    return 0


status = dispatch()
(ROOT / 'state.json').write_text(json.dumps(STATE))
raise SystemExit(status)
