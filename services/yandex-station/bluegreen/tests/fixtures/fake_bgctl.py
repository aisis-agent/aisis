"""Native-CI fixture: validate commands and keep desired/actual state independent."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).parent
ARGS = sys.argv[1:]
with (ROOT / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(['bg', *ARGS]) + '\n')
STATE = json.loads((ROOT / 'state.json').read_text())


def require(condition):
    if not condition:
        raise ValueError('unsupported fixture arguments: ' + repr(ARGS))


def idle_color():
    return STATE['idle_color']


def get_value():
    require(len(ARGS) == 3 and ARGS[1] == 'prod')
    key = ARGS[2]
    values = {'active': STATE['active']}
    for color in ('blue', 'green'):
        values['IMG_' + color.upper()] = STATE['images'][color]
        values['ARTIFACT_' + color.upper()] = STATE['artifacts'][color]
    require(key in values)
    print(values[key])


def recreate(color):
    require(color == idle_color())
    STATE['recreate_attempts'] += 1
    if os.environ.get('FAIL_BEFORE_RECREATE') == '1':
        print('fixture: compose failed before replacement', file=sys.stderr)
        return 1
    desired = {'image': STATE['images'][color], 'artifact': STATE['artifacts'][color]}
    if os.environ.get('FAIL_RESTORE') == '1' and desired['image'].startswith('previous-'):
        print('fixture: previous idle recreation failed', file=sys.stderr)
        return 1
    STATE['actual'] = desired
    STATE['actual_id'] = 'fixture-container-' + str(STATE['recreate_attempts'])
    if os.environ.get('FAIL_FIRST_RECREATE') == '1' and STATE['recreate_attempts'] == 1:
        print('fixture: candidate recreation failed after replacement', file=sys.stderr)
        return 1
    return 0


def compose():
    if ARGS[1:2] == ['stage']:
        require(ARGS[2:] in [['rm', '-sf', 'quasar-stage-blue', 'quasar-stage-green'],
                             ['up', '-d', '--force-recreate']])
        return 0
    require(ARGS[1:6] == ['prod', 'up', '-d', '--no-deps', '--force-recreate'] and len(ARGS) == 7)
    require(ARGS[6].startswith('quasar-prod-'))
    return recreate(ARGS[6].removeprefix('quasar-prod-'))


def set_image():
    require(len(ARGS) == 6 and ARGS[1] in ('stage', 'prod') and ARGS[2] in ('blue', 'green') and ARGS[4] == '--artifact')
    if ARGS[1] == 'prod':
        require(ARGS[2] == idle_color())
        STATE['images'][ARGS[2]] = ARGS[3]
        STATE['artifacts'][ARGS[2]] = ARGS[5]


def observation():
    require(len(ARGS) == 3 and ARGS[1] in ('prod', 'stage') and ARGS[2] in ('blue', 'green', 'switch'))
    if ARGS[1] == 'stage' or ARGS[2] == 'switch':
        require(ARGS[0] in ('verify-runtime', 'smoke'))
        return 0
    require(ARGS[2] == idle_color())
    if ARGS[0] == 'container-id':
        require(STATE['actual_id'] is not None)
        print(STATE['actual_id'])
    elif ARGS[0] == 'verify-runtime':
        expected = {'image': STATE['images'][ARGS[2]], 'artifact': STATE['artifacts'][ARGS[2]]}
        return int(STATE['actual'] != expected)
    elif ARGS[0] == 'smoke':
        return int(os.environ.get('FAIL_IDLE') == '1' and not STATE['actual']['image'].startswith('previous-'))
    return 0


def remove_inactive():
    require(ARGS[:3] == ['remove-inactive', 'prod', idle_color()] and len(ARGS) == 4)
    require(ARGS[2] != STATE['active'] and ARGS[3] == STATE['actual_id'])
    STATE['actual'] = None
    STATE['actual_id'] = None


def lifecycle():
    if ARGS[0] == 'wait':
        require(len(ARGS) == 4 and ARGS[1] in ('prod', 'stage') and ARGS[2] in ('blue', 'green') and ARGS[3] == '90')
        require(ARGS[1] == 'stage' or ARGS[2] == idle_color())
    elif ARGS[0] == 'flip':
        require(ARGS == ['flip', 'prod', idle_color()])
        STATE['active'] = ARGS[2]
    else:
        require(ARGS in [['render', 'stage'], ['devices', 'prod']])


def artifact():
    if ARGS[0] == 'import-artifact':
        require(ARGS == ['import-artifact', str(ROOT / 'remote-output')])
        print('/installed/new-artifact')
    else:
        require(ARGS == ['artifact-image', '/installed/new-artifact'])
        print('sha256:' + 64 * '1')


HANDLERS = {'get': get_value, 'compose': compose, 'set-image': set_image,
            'verify-runtime': observation, 'smoke': observation, 'container-id': observation,
            'remove-inactive': remove_inactive, 'wait': lifecycle, 'flip': lifecycle,
            'render': lifecycle, 'devices': lifecycle, 'import-artifact': artifact, 'artifact-image': artifact}
status = 2
try:
    require(bool(ARGS) and ARGS[0] in HANDLERS)
    status = HANDLERS[ARGS[0]]() or 0
except ValueError as error:
    print(error, file=sys.stderr)
finally:
    (ROOT / 'state.json').write_text(json.dumps(STATE))
raise SystemExit(status)
