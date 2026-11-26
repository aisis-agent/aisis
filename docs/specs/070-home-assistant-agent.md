# Home Assistant Conversational Domain

## Non-goal

AISIS does not replace `dext0r/yandex_smart_home` and does not reimplement native Yandex Smart Home exposure.

Direct commands such as “Алиса, включи свет” continue through the existing integration.

## Goal

Provide AI-capable Home Assistant tools for compound, contextual, analytical, historical, and multi-step requests from the central assistant.

## Connectivity

Home Assistant endpoints are deployment configuration, never repository constants.

Supported transport profiles are:

- `tailnet`: a private HTTP/HTTPS endpoint reachable from the paired ORC device or trusted server;
- `https`: a public HTTPS reverse proxy protected by Home Assistant bearer authentication.

On the local network AISIS finds Home Assistant itself (mDNS `_home-assistant._tcp`, and again when its address changes) and signs in through Home Assistant's own login and approval page, the standard OAuth authorization-code flow (`/auth/authorize`, `/auth/token`): the owner presses Connect and approves, and types no address and creates no token. The refresh token is stored privately (`0600`) and access tokens are refreshed from it automatically. Every component reaches Home Assistant through one client with the real Home Assistant REST/WebSocket API; there is no private proxy, cache or queue of service calls between them.

`HA_BASE_URL` and a long-lived `HA_TOKEN` (SecretRefs/environment settings) remain only for the `https` profile and for a network where discovery or the login flow cannot run; the connection page labels the pasted token as an advanced option. Public documentation uses non-live examples only.

The same typed HA adapter must work over both profiles. Tailnet/local access is preferred for private/high-trust operations when available.

## Runtime reuse

Hermes's HA integration is a reference for entity/service tools and filtered WebSocket `state_changed` events.

OpenClaw remains the central runtime; AISIS can implement HA as an OpenClaw tool/plugin or connect an existing supported HA/MCP integration.

## Tool groups

Read tools cover areas/entities, states/attributes, history/statistics, scripts/scenes metadata, services, and capability discovery.

Action tools cover service calls and validated compound plans.

The model receives filtered relevant state rather than the entire home state.

## Safety

Actions are risk-classified. Locks, alarms, garage doors, and similarly sensitive operations default to confirmation; from a voice surface that confirmation is an approval on an authenticated surface, never a spoken «да» (spec 020).

The existing native Yandex Smart Home path remains the fastest route for ordinary direct commands.
