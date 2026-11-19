"""Константы интеграции AISIS Speaker (локальный quasar-server, без облака Яндекса)."""

DOMAIN = "aisis_speaker"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_DUID = "duid"
CONF_TOKEN = "api_token"

DEFAULT_PORT = 31116
DEFAULT_SCAN_INTERVAL = 30

# директивы quasar-сервера (подтверждены GET /devices/:duid/directives)
DIRECTIVE_VOLUME = "smart_speaker_set_sound_volume"
DIRECTIVE_VOLUME_UP = "smart_speaker_set_sound_volume_louder"
DIRECTIVE_VOLUME_DOWN = "smart_speaker_set_sound_volume_quieter"
DIRECTIVE_PLAY = "smart_speaker_player_play"
DIRECTIVE_PAUSE = "smart_speaker_player_pause"
DIRECTIVE_NEXT = "smart_speaker_player_next_track"
DIRECTIVE_PREVIOUS = "smart_speaker_player_previous_track"
