"""Preserve the imported setup flow while flattening its existing branches."""

from unittest.mock import AsyncMock, patch

from custom_components.aisis_speaker.api import QuasarApiError
from custom_components.aisis_speaker.config_flow import ConfigFlowHandler

from ha_runtime import RealHaCase


class ConfigFlowAdoptionTests(RealHaCase):
    def flow(self):
        flow = ConfigFlowHandler()
        flow.hass = self.hass
        flow.context = {"source": "user"}
        return flow

    async def test_explicit_connected_fallback_and_empty_device_selection(self):
        devices = [
            {"duid": "first", "connected": False},
            {"duid": "second", "connected": True},
        ]
        for requested, observed, expected in (
            (" first ", devices, "first"),
            ("", devices, "second"),
            ("", devices[:1], "first"),
            ("", [], ""),
        ):
            with self.subTest(requested=requested, expected=expected):
                flow = self.flow()
                data = {"host": "fixture", "port": 9, "duid": requested}
                with patch.object(flow, "_fetch_devices", AsyncMock(return_value=observed)):
                    result = await flow.async_step_user(data)
                self.assertEqual(result["type"], "create_entry")
                self.assertEqual(
                    result["data"],
                    {"host": "fixture", "port": 9, "duid": expected, "token": ""},
                )

    async def test_missing_device_is_reported_without_entry(self):
        flow = self.flow()
        with patch.object(flow, "_fetch_devices", AsyncMock(return_value=[])):
            result = await flow.async_step_user(
                {"host": "fixture", "port": 9, "duid": "missing"}
            )
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"duid": "duid_not_found"})

    async def test_connection_failure_and_initial_form_preserved(self):
        flow = self.flow()
        initial = await flow.async_step_user()
        self.assertEqual(initial["type"], "form")
        self.assertEqual(initial["errors"], {})
        with patch.object(
            flow, "_fetch_devices", AsyncMock(side_effect=QuasarApiError("synthetic"))
        ):
            result = await flow.async_step_user({"host": "fixture", "port": 9})
        self.assertEqual(result["type"], "form")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
