"""Real Home Assistant runtime; only the external Quasar API is inert."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from homeassistant import const, loader
from homeassistant.config_entries import (
    ConfigEntries, ConfigEntry, ConfigEntryState, current_entry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import debounce
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import frame, restore_state, update_coordinator
from homeassistant.helpers.entity_platform import EntityPlatform

from custom_components import aisis_speaker as integration
from custom_components.aisis_speaker.api import QuasarApiError
from custom_components.aisis_speaker.const import DIRECTIVE_VOLUME, DOMAIN
from custom_components.aisis_speaker.media_player import AisisMediaPlayerEntity


class QuasarBoundary:
    """A synthetic device boundary, without HTTP or a real speaker."""

    def __init__(self):
        self.level = 4
        self.reported_level = None
        self.fail_push = False
        self.fail_devices = False
        self.fail_state = False
        self.pushes = []
        self.state_reads = 0
        self.active_reads = 0
        self.max_active_reads = 0
        self.read_started = asyncio.Event()
        self.read_gate = None

    async def get_devices(self):
        if self.fail_devices:
            raise QuasarApiError("synthetic device-list failure")
        return [{"duid": "fixture-speaker", "name": "Fixture", "connected": True}]

    async def get_state(self, duid):
        assert duid == "fixture-speaker"
        self.state_reads += 1
        self.active_reads += 1
        self.max_active_reads = max(self.max_active_reads, self.active_reads)
        observed = self.level if self.reported_level is None else self.reported_level
        self.read_started.set()
        try:
            if self.read_gate is not None:
                await self.read_gate.wait()
            if self.fail_state:
                raise QuasarApiError("synthetic state-read failure")
            return {"deviceState": {
                "SoundLevel": observed, "SoundMaxLevel": 10, "SoundMuted": False,
                "AudioPlayer": {"PlayerState": "PAUSED"},
            }}
        finally:
            self.active_reads -= 1

    async def push(self, duid, directives):
        assert duid == "fixture-speaker"
        self.pushes.extend(directives)
        if self.fail_push:
            raise QuasarApiError("synthetic push failure")
        for directive in directives:
            if directive["name"] == DIRECTIVE_VOLUME:
                self.level = directive["arguments"]["volume"]


class RealHaCase(unittest.IsolatedAsyncioTestCase):
    """Use the deployed HA coordinator, debouncer, platform and state machine."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if const.__version__ != "2026.9.3":
            raise AssertionError("This regression requires the deployed HA 2026.9.3")
        for module, expected in (
            (update_coordinator, "bd4b64d5b1ac623f29e70b1961023c28f562b61a77370a746159c3fd4a411ae0"),
            (debounce, "81093ac20caf91d4ea6b4c21d2e01b46f46c08f8c833447c512ea68437e3b3ac"),
        ):
            actual = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            if actual != expected:
                raise AssertionError(f"Deployed HA source differs: {module.__name__}")

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.tmp.name)
        loader.async_setup(self.hass)
        frame.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        await restore_state.async_load(self.hass, load_empty=True)
        self.entry = ConfigEntry(
            entry_id="aisis-volume-fixture",
            data={"host": "127.0.0.1", "port": 9, "duid": "fixture-speaker"},
            domain=DOMAIN, options={}, discovery_keys=MappingProxyType({}),
            subentries_data=[], title="Synthetic AISIS", source="user",
            version=1, minor_version=1, unique_id="fixture-speaker",
        )
        with patch.object(
            self.hass.config_entries, "async_setup", AsyncMock(return_value=True)
        ):
            await self.hass.config_entries.async_add(self.entry)
        self.client = QuasarBoundary()
        # ConfigEntry.async_setup supplies this same public context in production.
        self.entry._async_set_state(self.hass, ConfigEntryState.SETUP_IN_PROGRESS, None)
        entry_context = current_entry.set(self.entry)
        try:
            with (
                patch.object(integration, "QuasarClient", return_value=self.client),
                patch.object(integration, "async_get_clientsession", return_value=None),
                patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
            ):
                self.assertTrue(await integration.async_setup_entry(self.hass, self.entry))
        finally:
            current_entry.reset(entry_context)
        self.entry._async_set_state(self.hass, ConfigEntryState.LOADED, None)
        self.coordinator = self.hass.data[DOMAIN][self.entry.entry_id]["coordinator"]
        self.assertIs(self.coordinator.config_entry, self.entry)
        self.platform = EntityPlatform(
            hass=self.hass, logger=logging.getLogger(__name__),
            domain="media_player", platform_name=DOMAIN, platform=None,
            scan_interval=timedelta(seconds=30), entity_namespace=None,
        )
        self.platform.config_entry = self.entry
        self.player = AisisMediaPlayerEntity(
            self.coordinator, self.client, "fixture-speaker"
        )
        self.player.entity_id = "media_player.aisis_volume_fixture"
        await self.platform.async_add_entities([self.player])
        await self.hass.async_block_till_done()
        self.assertEqual(self.volume(), 0.4)

    def volume(self):
        state = self.hass.states.get(self.player.entity_id)
        self.assertIsNotNone(state)
        return state.attributes.get("volume_level")

    async def asyncTearDown(self):
        if self.client.read_gate is not None:
            self.client.read_gate.set()
        await self.coordinator.async_shutdown()
        await self.platform.async_reset()
        await self.hass.async_block_till_done()
        self.tmp.cleanup()
