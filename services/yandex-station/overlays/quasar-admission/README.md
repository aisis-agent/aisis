# Only the voice front and this host may talk to the quasar server

Ticket [#202](https://github.com/alex-mextner/aisis/issues/202) (retro-review [#186](https://github.com/alex-mextner/aisis/issues/186)). The same change is
the commit «fix: admit only known clients to the UniProxy and HTTP surface» of the public fork
[alex-mextner/aisis-quasar](https://github.com/alex-mextner/aisis-quasar); `prepare_overlay.py` applies it to the exact sources of the running image.

## Why this overlay exists

The quasar server answers **every** client that can open a TCP connection to its ports. Nothing authenticates a speaker:

| What a client can do, measured on the stage stack 2026-11-20 (dry-run Home Assistant) | Needs |
|---|---|
| open `/uni.ws` with any `x-uprx-device-id` and send a `TextInput`: the fast-intent layer runs it as if the speaker had said it (`Fast intent for text 'включи люстру'` → a Home Assistant service call, queued in the dry-run ha-cli) | an address, a throwaway id |
| the same on the colour's own port (`*:31315`, `*:31215`), bypassing the switch | an address |
| `GET /quasar.yandex.net/glagol/token?device_id=<anything>` returns a signed control token for that device (HTTP 200, a 212-character JWT) | an address |
| `GET /quasar.yandex.net/glagol/device_list` returns every speaker's `server_private_key` and `server_certificate` and its network info (HTTP 200) | an address |
| `GET /quasar.yandex.net/get_sync_info?device_id=<anything>` creates a station record (the WebSocket does the same on connect) | an address |
| `GET :31114/state`, `/functions` (function-state server, listens on all interfaces) read the other speakers' state | an address |

Who can reach them (verified 2026-11-20, read-only): the switch listens on `0.0.0.0:31115` and `0.0.0.0:31114`, the colours on `*:312xx/313xx` (stage `315xx/316xx`);
the quasar API `:31116` is loopback, but `aisis-station-lan-proxy` forwards it to the LAN address (separate change, below). That is every device on the flat home LAN
(about 28 devices in the router's inventory: phones, IoT plugs, a Chromecast, a Kodi box, three speakers), every peer of the tailnet that the tailnet policy lets
reach this host (two of the nodes belong to other tailnet users), and every container with a bridge network on this host. The router forwards no port to this host
(one forward, to Home Assistant), has no DMZ host, and the host has no global IPv6 address; Tailscale serve and funnel are not configured. Not verified: the
host firewall rules (reading them needs root) and the tailnet access policy.

Reach of a text turn: the fast-intent layer's Home Assistant domains in this home are lights (the chandelier, the garland, the globe), the kettle's smart plug
(`switch.kettle_plug_local`), the two air conditioners, the TV and the projector (power, volume, media keys), the TV-app and fireplace scripts, speaker volume,
music through the adapter, and the dashboard settings that can be changed by voice. There is **no** lock, alarm panel, garage door or cover in this Home Assistant
(snapshot of its entity registry: 12 lights, 12 switches, 2 climate, 16 media players, no `lock`, `alarm_control_panel`, `cover`). The glagol routes are worse than a text
turn: a control token plus a speaker's private key and address let a LAN client use that speaker's local control channel.

## The change (three files in the image)

1. NEW `admission.ts`: `AISIS_ALLOWED_CLIENTS` (comma separated IPs and CIDRs, the voice front the speakers connect through), `AISIS_EDGE_TOKEN` (optional second factor
   in the `x-aisis-edge-token` header), `AISIS_ADMISSION=enforce|log`. Policy: the client address is the `X-Real-IP` of a loopback peer (the switch sets it from the address it
   accepted; nginx replaces a client-supplied copy: checked with the switch image) or the peer itself on a direct connection; headers of a non-loopback peer are never believed. This host
   is always admitted; anyone else must be in the list (nobody, if unset) and, on a WebSocket, must name a station this server already knows (no provisioning by a text turn).
   A failing station lookup refuses. A refusal is HTTP 403 before the upgrade or the route, logged once a minute per reason.
2. PATCH `main.ts` (four exact blocks): admission is the first Express middleware (before body parsing and every route; an HTTP `device_id` from another address must be a known station too); the HTTP
   server binds to `BIND_HOST` (default `127.0.0.1`: a colour is reached through the switch on the same host, so the colours' own ports are no longer reachable from the LAN) and the function-state server always to `127.0.0.1`.
3. PATCH `routers/uniproxy.alice.yandex.net.ts` (three exact blocks): the upgrade is admitted before it becomes a connection.

`bluegreen/compose.bluegreen.yaml` passes `AISIS_ALLOWED_CLIENTS` from `~/.config/aisis/runtime.env` and **requires** it for prod (`:?`): a missing value stops the deploy
at `compose up`, not the speakers after the flip. Stage admits loopback only.

## Build, check, deploy

```
./run_check.sh                          # builds from the active colour (SHA-256 pinned, no fuzz); in scratch containers with no network: eslint, then
                                        # check_admission.mts 59/59 with the overlay, and the same check WITHOUT it (the unpatched router and main.ts
                                        # never consult the policy) which must fail
./run_check.sh --router CLOSE_LOG_OUT/routers/uniproxy.alice.yandex.net.ts   # the router after the uniproxy-close-log overlay (its own check 33/33 still passes on the combined router)
python3 -m unittest discover -s . -p "test_*.py"   # AISIS_QUASAR_BASELINE_DIR=<docker cp of /src> [AISIS_QUASAR_CLOSE_LOG_ROUTER=… AISIS_FORK_SRC=<fork>/src] adds the real-file tests
```

`prepare_overlay.py --baseline DIR --output-dir OUT [--router FILE]` writes `admission.ts`, `main.ts` and `routers/uniproxy.alice.yandex.net.ts`: copy them into the combined
overlay directory (the router file is the one the close-log overlay produced when that overlay is in the same build; `--router` takes it). Pinned: pristine `main.ts` `dc56a983…` → `d4b49d99…`;
pristine router `84a65653…` → `ea1c6ba3…`; router after close-log `34f1c54d…` → `5fd84c5e…`; `admission.ts` `75ebda5b…`.

The deploy is part of ONE combined flip with the other pending overlays (uniproxy-close-log, ha-verify-wording, correction-hook, the AC overlay): build the combined overlay directory,
prebuild the image, run the corpus on it and collect the receipt (`../../corpus-runner/README.md`), then `deploy.sh deploy --image "$VERIFIED_IMAGE_ID" --corpus-receipt "$CORPUS_RECEIPT"`.

**Before the flip (owner/deploy owner):**

1. Find the voice front. The only legitimate non-local client of `:31115` is the host the speakers' TLS connection comes through. Sample for a few minutes while the speakers
   are used: `for i in $(seq 600); do ss -tnH state established '( sport = :31115 )' | awk '{print $4}' | sed 's/:[0-9]*$//'; sleep 1; done | sort | uniq -c`
   (an address that connects once per exchange shows up only while a dialog runs; sample over an evening, and ask the owner for any other client such as a Home Assistant
   integration that calls `:31115` directly).
2. Put it in `~/.config/aisis/runtime.env`: `AISIS_ALLOWED_CLIENTS=<voice-front-address>[,<other>…]`. Without it `compose up` stops with «set the voice front address».
3. Optional first pass: `AISIS_ADMISSION=log` in `runtime.env` logs `Admission would refuse (AISIS_ADMISSION=log) …` for every client enforcement would refuse and refuses nothing:
   run one evening, read `docker logs aisis-quasar-prod-<colour> 2>&1 | grep "Admission would refuse"`, add what is legitimate, then remove the line and recreate the colour (a container
   reads its environment once).
4. Every station that talks to this server must already be in its database (they are: the speakers have been connecting for weeks). A **new** speaker is provisioned from this host
   before it first connects: `curl -s "http://127.0.0.1:31115/quasar.yandex.net/get_sync_info?device_id=<station id>&platform=<platform>" >/dev/null` (loopback is admitted).

**After the flip:** the new colour logs one line at start, `Admission enforced: loopback, Address: IPv4 <voice-front-address>` (check it first: a missing address is the usual mistake);
from a LAN device other than the voice front, `probe_text_turn.mts` (here) against `:31115` must say `refused: HTTP 403`, and
`curl -s -o /dev/null -w '%{http_code}' http://<host>:31115/quasar.yandex.net/glagol/device_list` must print `403`; from the voice front a speaker's wake word and a spotter command
(«включи свет») must still work. `docker logs … | grep "Admission refused"` shows who was turned away.

Rollback: `deploy.sh rollback` (the previous image has no admission; the env var is ignored there). If the speakers stop answering after the flip, the first suspect is a front address
missing from `AISIS_ALLOWED_CLIENTS`: the log line says which client address was refused.

**The warm standby colour is not covered by this flip.** `deploy.sh deploy` updates only the idle colour and keeps the previous image running as the rollback target and as nginx's backup
upstream; that old image has no admission and listens on `*:31215/31315` (and `*:31214/31314`). Until the firewall rules below are applied, a LAN client can bypass the patched active colour
through the standby's own port. Treat the firewall rules (or recreating the standby colour on the patched image once the flip is verified) as a **cutover requirement**, and verify it:
`nc -vz <host> 31215` and `nc -vz <host> 31315` must fail from a LAN device other than the front. Likewise the switch's function-state listener (`0.0.0.0:31114`, `bgctl.py`) has no
admission: it is covered only by the firewall rules, or by binding it to loopback in `bgctl.py` (not changed here: whether anything off this host calls `:31114` is not verified).

## Do these today, without the flip (owner/Main; nothing in this overlay changes them)

The exposure is wider than it needs to be and the quasar change only lands with the combined flip. A host firewall rule closes it now and stays as defence in depth afterwards (host
networking: the containers' ports are host ports, so `ufw` filters them). Replace the placeholders; `ufw` evaluates rules in insertion order, so the allows come first:

```
sudo ufw allow from <voice-front-address> to any port 31115 proto tcp comment 'aisis voice front'
sudo ufw deny in on enp4s0 to any port 31114,31115,31214,31215,31314,31315,31514,31515,31614,31615 proto tcp
sudo ufw deny in on tailscale0 to any port 31114,31115,31214,31215,31314,31315,31514,31515,31614,31615 proto tcp
sudo ufw status numbered      # read it back; `sudo ufw delete <n>` removes a rule
```

(`enp4s0` is this host's LAN interface; `ip -br addr` names it.) Check from the voice front that a speaker still answers, and from another LAN device that `nc -vz <host> 31115` fails.
The quasar API proxy (`:31116`) is restricted in code, below.

## The LAN proxy (`aisis-station-lan-proxy`, same PR, deployed independently of the flip)

`quasar_lan_proxy.py` forwarded `LAN:31116` to the quasar **API** for any client; that API has no authentication and can make every speaker say a text (`POST /devices/all/push/text`),
send a raw directive (`/push/raw`) and rename or reconfigure a speaker (`PATCH /devices/<duid>`). It now accepts only `ALLOWED_PEERS` (addresses/CIDRs, from
`~/.config/aisis/lan-proxy.env`, read by the unit) and refuses to start without it (`ALLOWED_PEERS=0.0.0.0/0` keeps the old open behaviour on purpose). To deploy:

```
printf 'ALLOWED_PEERS=<home-assistant-address>\n' > ~/.config/aisis/lan-proxy.env && chmod 600 ~/.config/aisis/lan-proxy.env
~/xp/aisis/scripts/install-systemd-units.sh             # installs the changed unit, never restarts a running one
systemctl --user restart aisis-station-lan-proxy.service
journalctl --user -u aisis-station-lan-proxy.service -n 5   # "peers: <address>"; a refused client logs "refused <address>: not in ALLOWED_PEERS"
```

Check that Home Assistant still reaches the speaker (a text-to-speech call from Home Assistant to a speaker); `aisis-failover` probes this unit.

## What this does not cover

* The speaker-facing kit (`aisis-station-kit` `:18080/:18443`, bound to the LAN address) and the dashboard (`:8781`, all interfaces) are other services with their own exposure; the
  kit's overlay problem is [#189](https://github.com/alex-mextner/aisis/issues/189).
* **The voice front launders its clients.** Everything the front forwards arrives from the front's address, so a LAN device that talks to the front directly (instead of the speaker) is admitted like the speaker
  unless the front itself only accepts the speakers' addresses; the known-station rule (WebSocket id, HTTP `device_id`) is what is left against it, and `GET /quasar.yandex.net/glagol/device_list` has no
  device id. Closing this needs the front's own access list (a separate host) or `AISIS_EDGE_TOKEN` with the front adding `x-aisis-edge-token` for the speakers' addresses only. Not done here.
* A client that can spoof the voice front's address on the LAN: same remedy (`AISIS_EDGE_TOKEN`).
* The HTTP routes that Yandex's own apps would call stay as upstream wrote them for an admitted client (`/glagol/*` do not authenticate a caller: admission is the boundary).
