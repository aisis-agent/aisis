"""The STT gateway tells the TV pause which station opened the turn (x-aisis-station header or ?station=), so the pause can be scoped to
that speaker's room (#276). The pause runs in a worker thread and never delays the turn."""
import asyncio
import sys
import types
import unittest

from aiohttp.test_utils import TestClient, TestServer

try:
    import numpy  # noqa: F401  (the gateway's level front-end needs it; only the import matters here)
except ImportError:
    sys.modules.setdefault("speech_frontend", types.ModuleType("speech_frontend"))   # CI has no numpy

import stt_wyoming


class RecordingTv:
    def __init__(self):
        self.stations = []

    def activation(self, station=None):
        self.stations.append(station)


class ActivationReachesTheTvPause(unittest.TestCase):
    def open_turn(self, headers=None, query=""):
        tv = RecordingTv()

        async def play():
            gateway = stt_wyoming.STTGateway(env={"AISIS_WYOMING_URI": "tcp://127.0.0.1:1", "AISIS_STT_DSP": "0", "AISIS_STT_WAKE_WINDOW": "0"}, tv=tv, archive=False, domain_catalog=None)
            async with TestClient(TestServer(gateway.app())) as client:
                async with client.ws_connect("/" + query, headers=headers or {}) as ws:
                    await ws.send_json({"sample_rate": 16000})
                    for _ in range(100):
                        if tv.stations:
                            break
                        await asyncio.sleep(0.02)
                    await ws.send_json({"type": "finish"})

        asyncio.run(play())
        return tv.stations

    def test_the_station_header_is_handed_over(self):
        self.assertEqual(self.open_turn(headers={"x-aisis-station": "M00ZRW3008RAJG"}), ["M00ZRW3008RAJG"])

    def test_the_station_query_is_handed_over(self):
        self.assertEqual(self.open_turn(query="?station=M10DST310HXNPK"), ["M10DST310HXNPK"])

    def test_a_turn_that_names_no_station_hands_over_none(self):
        self.assertEqual(self.open_turn(), [None])


if __name__ == "__main__":
    unittest.main()
