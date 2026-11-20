"""Acceptance harness of the recogniser switch (ticket #59): fixture builders, Wyoming batch runner and the three tiers.

Every module talks to recognisers only through the Wyoming protocol, with `stt_wyoming.transcribe` (the gateway's own client), and
never replays audio through the live gateway. Audio and results live in a working directory the caller names (on disk, not on /tmp).
"""
