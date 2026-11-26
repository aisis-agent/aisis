#!/usr/bin/env python3
"""Installer of the Home Assistant TV auto-pause (ticket #320): the versioned automation file `ha-automations/tv_autopause.json` is filled from a
`Spec` and created through Home Assistant's config API on the shared client (ha_client.py), with no hand-typed address or key.

    python3 ha_autopause.py install     create the three helpers and the two automations; a second run changes nothing
    python3 ha_autopause.py enable      THE one line that turns the auto-pause on (the master switch helper; install leaves it off)
    python3 ha_autopause.py disable     off again
    python3 ha_autopause.py status      helpers and automations as Home Assistant has them
    python3 ha_autopause.py uninstall   remove the helpers and both automations

What it installs (the behaviour is in the JSON; AISIS fires the events, ha_events.py): `aisis_listening` pauses a playing TV; `aisis_dialog_ended`
of that activation (or `max_hold_s` after the last listening) resumes what the automation itself paused, if still paused and the resume switch is
on; the owner's own action on the TV during the hold, or a Home Assistant restart, releases it. State lives in three input_boolean helpers
(enabled, resume, we_paused), the timers are Home Assistant's run (`wait_for_trigger` timeout, `delay`); no script, service or process of AISIS
holds anything.

A helper is created in its `new_state` only when it does not exist: a reinstall never resets the owner's switches. Needs a connected Home
Assistant (`AISIS_HA_STORE` or the default store of ha_client.py).
"""
from __future__ import annotations

import argparse
import json
import re
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import ha_client
import ha_events
import ha_ws

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "ha-automations" / "tv_autopause.json"
WHOLE_TOKEN = re.compile(r"^\$\{(\w+)\}$")


class InstallError(Exception):
    """Home Assistant answered, and what it answered is not what the installer needs."""


@dataclass(frozen=True)
class Spec:
    """One auto-pause: the player it watches, the services that pause and resume it, and what counts as the owner's own call on it."""

    slug: str = "tv_autopause"
    title: str = "TV autopause"
    player: str = "media_player.televizor"
    player_domain: str = "media_player"
    pause_script: str = "script.tv_pause"
    play_script: str = "script.tv_play"
    watch: tuple[str, ...] = ("media_player.televizor", "media_player.ue48j5500_ue48j5500", "script.tv_pause", "script.tv_play")
    listening_event: str = ha_events.LISTENING
    ended_event: str = ha_events.DIALOG_ENDED
    max_hold_s: int = 300
    grace_ms: int = 1500

    def params(self) -> dict[str, Any]:
        values = {"slug": self.slug, "title": self.title, "player": self.player, "player_domain": self.player_domain,
                  "pause_script": self.pause_script, "play_script": self.play_script, "listening_event": self.listening_event,
                  "ended_event": self.ended_event, "max_hold_s": self.max_hold_s, "grace_ms": self.grace_ms}
        values["watch"] = "[" + ", ".join(repr(entity) for entity in self.watch) + "]"
        return values

    def helper(self, key: str) -> str:
        return f"input_boolean.{self.slug}_{key}"


@dataclass(frozen=True)
class Helper:
    kind: str
    entity: str
    name: str
    new_state: str


@dataclass(frozen=True)
class Plan:
    helpers: list[Helper]
    automations: list[dict[str, Any]]


@dataclass
class Report:
    created: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def _fill(node: Any, params: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        return {key: _fill(value, params) for key, value in node.items()}
    if isinstance(node, list):
        return [_fill(item, params) for item in node]
    if isinstance(node, str):
        whole = WHOLE_TOKEN.match(node)
        if whole:
            return params[whole.group(1)]
        return string.Template(node).substitute({key: str(value) for key, value in params.items()})
    return node


def render(spec: Spec, template: Path = TEMPLATE_PATH) -> Plan:
    body = _fill(json.loads(template.read_text(encoding="utf-8")), spec.params())
    helpers = [Helper(item["kind"], f"{item['kind']}.{item['id']}", item["name"], item["new_state"]) for item in body["helpers"]]
    return Plan(helpers, body["automations"])


class Api(Protocol):
    def helper_ids(self, kind: str) -> set[str]: ...
    def create_helper(self, kind: str, name: str) -> str: ...
    def delete_helper(self, entity: str) -> None: ...
    def set_helper(self, entity: str, on: bool) -> None: ...
    def get_automation(self, config_id: str) -> dict[str, Any] | None: ...
    def put_automation(self, config_id: str, config: dict[str, Any]) -> None: ...
    def delete_automation(self, config_id: str) -> None: ...
    def reload_automations(self) -> None: ...


def _discard(api: Api, entity: str) -> None:
    """Best effort: a helper this run created and could not finish must not stay behind looking installed."""
    try:
        api.delete_helper(entity)
    except (ha_client.HaError, InstallError):
        pass


def _discard_if_created(api: Api, helper: Helper) -> None:
    try:
        if helper.entity in api.helper_ids(helper.kind):
            api.delete_helper(helper.entity)
    except (ha_client.HaError, InstallError):
        pass


def install(api: Api, spec: Spec) -> Report:
    plan = render(spec)
    report = Report()
    present: dict[str, set[str]] = {}
    for helper in plan.helpers:
        if helper.kind not in present:
            present[helper.kind] = api.helper_ids(helper.kind)
        if helper.entity in present[helper.kind]:
            continue
        try:
            made = api.create_helper(helper.kind, helper.name)
        except (ha_client.HaError, InstallError):
            _discard_if_created(api, helper)       # the answer may have been lost after Home Assistant made it
            raise
        try:
            if made != helper.entity:
                raise InstallError(f"Home Assistant named the helper {made}, expected {helper.entity}")
            if helper.new_state == "on":
                api.set_helper(helper.entity, True)
        except (ha_client.HaError, InstallError):
            _discard(api, made)
            raise
        report.created.append(helper.entity)
    for config in plan.automations:
        if api.get_automation(config["id"]) != config:
            api.put_automation(config["id"], config)
            report.written.append(config["id"])
    if report.written:
        api.reload_automations()
    return report


def set_enabled(api: Api, spec: Spec, on: bool) -> None:
    entity = spec.helper("enabled")
    if entity not in api.helper_ids("input_boolean"):
        raise InstallError(f"{entity} does not exist: run install first")
    api.set_helper(entity, on)


def uninstall(api: Api, spec: Spec) -> Report:
    plan = render(spec)
    report = Report()
    for config in plan.automations:
        if api.get_automation(config["id"]) is not None:
            api.delete_automation(config["id"])
            report.deleted.append(config["id"])
    if report.deleted:
        api.reload_automations()
    present = api.helper_ids("input_boolean")
    for helper in plan.helpers:
        if helper.entity in present:
            api.delete_helper(helper.entity)
            report.deleted.append(helper.entity)
    return report


class HaApi:
    """The installer's calls on Home Assistant's real API: REST for the automation configs and services, the WebSocket for helpers."""

    def __init__(self, client: ha_client.HaClient | None = None) -> None:
        self.client = client or ha_client.default()

    def ws(self, type_: str, **fields: Any) -> Any:
        session = ha_ws.Session.open(self.client)
        try:
            wanted = session.send(type_, **fields)
            while True:
                message = session.recv(5.0)
                if message is None:
                    raise InstallError(f"Home Assistant did not answer {type_}")
                if message.get("type") == "result" and message.get("id") == wanted:
                    if not message.get("success"):
                        raise InstallError(f"{type_}: {(message.get('error') or {}).get('message')}")
                    return message.get("result")
        finally:
            session.close()

    def _ok(self, reply: ha_client.Reply, what: str) -> ha_client.Reply:
        if not reply.ok:
            raise InstallError(f"{what}: HTTP {reply.status} {reply.error or reply.text()[:200]}")
        return reply

    def helper_ids(self, kind: str) -> set[str]:
        return {f"{kind}.{item['id']}" for item in self.ws(f"{kind}/list")}

    def create_helper(self, kind: str, name: str) -> str:
        return f"{kind}.{self.ws(f'{kind}/create', name=name)['id']}"

    def delete_helper(self, entity: str) -> None:
        kind, _, object_id = entity.partition(".")
        self.ws(f"{kind}/delete", **{f"{kind}_id": object_id})

    def set_helper(self, entity: str, on: bool) -> None:
        kind = entity.partition(".")[0]
        self._ok(self.client.service(kind, "turn_on" if on else "turn_off", {"entity_id": entity}), f"{kind}.turn_{'on' if on else 'off'}")

    def get_automation(self, config_id: str) -> dict[str, Any] | None:
        reply = self.client.get(f"/api/config/automation/config/{config_id}")
        if reply.status == 404:
            return None
        return self._ok(reply, f"read automation {config_id}").json()

    def put_automation(self, config_id: str, config: dict[str, Any]) -> None:
        self._ok(self.client.post(f"/api/config/automation/config/{config_id}", config), f"write automation {config_id}")

    def delete_automation(self, config_id: str) -> None:
        self._ok(self.client.request("DELETE", f"/api/config/automation/config/{config_id}"), f"delete automation {config_id}")

    def reload_automations(self) -> None:
        self._ok(self.client.service("automation", "reload"), "automation.reload")


def status(api: Api, spec: Spec) -> dict[str, Any]:
    plan = render(spec)
    present = api.helper_ids("input_boolean")
    return {"helpers": {helper.entity: helper.entity in present for helper in plan.helpers},
            "automations": {config["id"]: api.get_automation(config["id"]) == config for config in plan.automations}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the Home Assistant TV auto-pause through the config API.")
    parser.add_argument("command", choices=["install", "enable", "disable", "status", "uninstall"])
    args = parser.parse_args(argv)
    api, spec = HaApi(), Spec()
    try:
        if args.command == "install":
            report = install(api, spec)
            print(json.dumps({"helpers_created": report.created, "automations_written": report.written}, ensure_ascii=False))
        elif args.command in ("enable", "disable"):
            set_enabled(api, spec, args.command == "enable")
            print(f"{spec.helper('enabled')}: {'on' if args.command == 'enable' else 'off'}")
        elif args.command == "status":
            print(json.dumps(status(api, spec), ensure_ascii=False, indent=1))
        else:
            report = uninstall(api, spec)
            print(json.dumps({"deleted": report.deleted}, ensure_ascii=False))
    except (ha_client.HaError, InstallError) as err:
        print(f"ha_autopause: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
