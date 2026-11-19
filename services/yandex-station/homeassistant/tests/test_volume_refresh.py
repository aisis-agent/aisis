"""Volume commands must publish observations without HA's ten-second debounce."""

import asyncio
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError

from ha_runtime import RealHaCase


class VolumeRefreshTests(RealHaCase):
    async def test_consecutive_zero_restore_publishes_observed_state(self):
        observed = []

        @callback
        def changed(event):
            state = event.data.get("new_state")
            if state and state.entity_id == self.player.entity_id:
                observed.append(state.attributes.get("volume_level"))

        unsubscribe = self.hass.bus.async_listen("state_changed", changed)
        self.addCleanup(unsubscribe)
        reads = self.client.state_reads
        async with asyncio.timeout(2):
            await self.player.async_set_volume_level(0)
            await self.hass.async_block_till_done()
            self.assertEqual(self.volume(), 0)
            await self.player.async_set_volume_level(0.4)
            await self.hass.async_block_till_done()
            self.assertEqual(
                self.volume(), 0.4,
                "HA volume stayed stale inside the default ten-second debounce",
            )
        self.assertEqual(self.client.state_reads, reads + 2)
        self.assertEqual(observed, [0, 0.4])
        self.assertEqual([x["arguments"]["volume"] for x in self.client.pushes], [0, 4])
        self.assertEqual(self.coordinator.update_interval, timedelta(seconds=30))

    async def test_volume_during_another_command_cooldown_is_observed(self):
        await self.player.async_media_pause()
        reads = self.client.state_reads
        await self.player.async_set_volume_level(0)
        await self.hass.async_block_till_done()
        self.assertEqual(self.volume(), 0)
        self.assertEqual(self.client.state_reads, reads + 1)

    async def test_other_commands_keep_existing_debounce(self):
        reads = self.client.state_reads
        await self.player.async_media_pause()
        await self.player.async_media_play()
        self.assertEqual(self.client.state_reads, reads + 1)
        self.assertEqual(len(self.client.pushes), 2)

    async def test_push_failure_keeps_last_observation(self):
        reads = self.client.state_reads
        self.client.fail_push = True
        with self.assertRaises(HomeAssistantError):
            await self.player.async_set_volume_level(0)
        self.assertEqual(self.volume(), 0.4)
        self.assertEqual(self.client.state_reads, reads)
        self.assertEqual(len(self.client.pushes), 1)

    async def test_device_list_failure_does_not_confirm_requested_volume(self):
        self.client.fail_devices = True
        await self.player.async_set_volume_level(0)
        await self.hass.async_block_till_done()
        self.assertFalse(self.coordinator.last_update_success)
        self.assertFalse(self.player.available)
        self.assertEqual(self.player.volume_level, 0.4)
        self.assertEqual(self.hass.states.get(self.player.entity_id).state, "unavailable")

    async def test_failed_state_read_does_not_invent_volume(self):
        self.client.fail_state = True
        await self.player.async_set_volume_level(0)
        await self.hass.async_block_till_done()
        self.assertTrue(self.coordinator.last_update_success)
        self.assertIsNone(self.coordinator.data["state"])
        self.assertIsNone(self.player.volume_level)
        self.assertIsNone(self.volume())

    async def test_stale_device_readback_stays_stale(self):
        self.client.reported_level = 4
        await self.player.async_set_volume_level(0)
        await self.hass.async_block_till_done()
        self.assertEqual(self.volume(), 0.4)
        self.assertEqual(self.client.level, 0)
        self.assertEqual(len(self.client.pushes), 1)

    async def test_human_volume_change_is_published_as_observed(self):
        self.client.reported_level = 7
        await self.player.async_set_volume_level(0)
        await self.hass.async_block_till_done()
        self.assertEqual(self.volume(), 0.7)
        self.assertEqual(len(self.client.pushes), 1)

    async def test_volume_refresh_uses_real_coordinator_serialization(self):
        self.client.read_gate = asyncio.Event()
        self.client.read_started.clear()
        refresh = asyncio.create_task(self.coordinator.async_refresh())
        await self.client.read_started.wait()
        volume = asyncio.create_task(self.player.async_set_volume_level(0))
        try:
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertFalse(volume.done())
            self.assertEqual(self.volume(), 0.4)
            self.assertEqual(self.client.max_active_reads, 1)
        finally:
            self.client.read_gate.set()
            await asyncio.gather(refresh, volume)
        await self.hass.async_block_till_done()
        self.assertEqual(self.volume(), 0)
        self.assertEqual(self.client.max_active_reads, 1)
