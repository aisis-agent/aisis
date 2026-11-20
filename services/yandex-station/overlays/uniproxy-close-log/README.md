# A closed UniProxy WebSocket is logged with its close code, and a normal one is not a warning

`routers/uniproxy.alice.yandex.net.ts` (the quasar's UniProxy server, which the speaker's WebSocket talks to) logged
`UniProxy WebSocket closed` at **WARN** for every connection that ended, with no close code, no device and no age.

## Why this overlay exists

The speaker ends a connection after every exchange, and about 0.6 s after the spotter-log events (`LogSpotter`) of a wake word
(200 of 395 closes follow a `LogSpotter` line within 2 s); the deploy smoke checks (`bluegreen/smoke.py`, throwaway device
`bg-smoke-…`: two closes per deploy) and the failover drill open and close theirs too. None of those is a failure, yet each was a
WARN. The dashboard's log parser (`dialogstore.py`) used to take every WARN that fell into an open dialog for that dialog's failure,
so the session page and the dashboard showed `ошибка: uniproxy.alice.yandex.net.ts:49: UniProxy WebSocket closed` and a red dialog
for an exchange that went fine (owner tg#30668). Measured 2026-11-19: 395 closes in 13 days of the prod containers' logs (36 in the
last 24 h); in the stored history 173 of 492 dialogs carried that single line as their error (35 %; 198 dialogs were flagged in
all), 24 of the 40 dialogs of the last 24 h. It was never a reconnect storm: the server side has no reconnect logic, the rate
follows the speaker's use and the deploys (the busiest hour, 24 closes in the hour of 2026-11-09 00:00 UTC, had four color restarts in it).

A passive capture of the nginx → quasar leg during a real activation (2026-11-19 14:44, WebSocket close frames only, no payload kept)
showed the speaker's dialog connection ending with a proper close frame, **code 1000**.

## The change

Two texts replace the one line (`routers/uniproxy.alice.yandex.net.ts`, the `close` handler and a pure `closeLogLine` helper):

| peer's close code | level | text |
|---|---|---|
| 1000, 1001, 1005 (the peer ended it itself; 1005 = closed with no status) | INFO | `UniProxy WebSocket closed (normal): code=1000 device=<id> age=<n>ms` |
| anything else (1006: dropped with no close frame; 1002, 1003, 1007, 1008, 1009, 1011, 4xxx) | WARN | `UniProxy WebSocket closed abnormally: code=1006 device=<id> age=<n>ms [reason="…"]` |

The device id is sanitized (a request header) and the reason is quoted, cut to 80 characters (peer text). `UniProxy WebSocket error: …`
(the socket's own `error` event) is unchanged, still WARN. `dialogstore.py` (`WS_CLOSE_RE`) reads exactly these two texts and the old one
without a code, so the dashboard is correct before **and** after this overlay is deployed.

## Build, check, deploy

`deploy.sh deploy` builds `FROM` the active color's image, so the other overlays (speaker-room, intent-grammar, weather-via-cli,
volume) carry over: this overlay replaces one file none of them touches. The candidate image is new, so the deploy needs
corpus evidence for exactly that image ID (`--corpus-receipt`, [corpus-runner/README.md](../../corpus-runner/README.md)): build the
candidate once, prove it, then deploy it by its immutable ID. Its `/src` differs from the active image's by exactly this one file
(`routers/uniproxy.alice.yandex.net.ts`, hashes below).

```
overlay=$(./run_check.sh 2>/dev/null | tail -n 1)   # builds the overlay from the active color's file (SHA-256 checked, no fuzz), then in
                                                    # scratch containers with no network: the image's own eslint on the patched file and
                                                    # check_close_logging.mts; the last line is the overlay directory
ctx=$(mktemp -d) && cp -a "$overlay" "$ctx/src" && printf 'FROM %s\nCOPY src /src\n' "$ACTIVE_IMAGE" >"$ctx/Dockerfile"
docker build -q -t aisis-quasar:bg-uniproxy-close-log "$ctx"
../../bluegreen/deploy.sh deploy --image "$(docker image inspect --format '{{.Id}}' aisis-quasar:bg-uniproxy-close-log)" --corpus-receipt "$CORPUS_RECEIPT"
```

`check_close_logging.mts` connects real WebSocket clients to the real router (the logger and `UniProxyConnection` are stand-ins) and
ends each connection in each way: `close(1000)`, `close(1001)`, `close()` (→ 1005), `terminate()` (→ 1006), `close(1011)`, `close(1008)`,
`close(4000)`, an odd device id and reason, a connection without a device id. Against the unpatched image it prints `ИТОГ 15/33`; with
the overlay `ИТОГ 33/33`. `deploy.sh` then runs its stage checks and the idle color's smoke, and flips only if they pass.

After the flip, the first hour of `docker logs --since 1h aisis-quasar-prod-<color>` split by `grep -c 'WebSocket closed (normal)'` and
`grep -c 'WebSocket closed abnormally'` shows what the speaker really sends: a steady `abnormally` count would mean it drops connections
and is worth its own look; `dialogstore.py` shows it as an error then, and only then.

## Provenance

Baseline: `/src/routers/uniproxy.alice.yandex.net.ts` of the active image (`aisis-quasar:volume-zero-e859a37b`; the same bytes are in
`aisis-quasar:bg-weather-via-cli-20261117`), SHA-256 `84a656535de98f8a239c1d54aa70821a8d8f420d4f7424d77377b9764958da71`; patched
`34f1c54d257a05e35c48404942d33d49c96d3d01a853fc25443b9db77a5e69ba`. After a later deploy changes the file the hash changes and
`prepare_overlay.py` refuses until the patch is rebased: it must never be applied blind.

## Tests

`python3 -m unittest discover -s services/yandex-station/overlays/uniproxy-close-log` (patch applier, run by CI);
`check_close_logging.mts` (inside the image, `run_check.sh`); the log parser side: `services/yandex-station/code/test_dialogstore_log_noise.py`.
