# CCX Bridge Home Assistant add-on

An ingress-only dashboard for the C6 bridge gateway. The browser talks only to this add-on; the gateway bearer token remains in `/data/options.json` and is sent by the Python proxy. The proxy accepts requests only from the Home Assistant ingress peer (`172.30.32.2`) and forwards a fixed set of API routes to the configured gateway origin.

## Setup

Add `https://github.com/thetimetraveler/c6-live-bridge` through the Home Assistant app store's **Repositories** menu, then install **CCX Bridge**. Set `gateway_host` to the gateway's HTTP(S) origin (default `http://lutron-bridge.local:8080`, no path/query), and set `gateway_token` to its bearer token. A background poller archives events even when the dashboard is closed. The add-on writes bounded daily snapshots to `/share/c6-management/events`, includes HA receive time, and prunes files older than seven days.

The dashboard targets one-second live refreshes and shows a topology strip from Thread through the endpoint, UART, gateway and N4, with the report-return path. Flow pulses are tied to observed event stages; the diagram never infers a successful PUT or report from connectivity alone. It shows event cursor gaps, requested and confirmed endpoint values separately, gateway-uptime-based freshness (stale after 120 seconds), a read-only programming snapshot on page 0, mapping configuration, and endpoint firmware upload. Firmware uploads are capped at 786,432 bytes. No demo device data is bundled.

## Home Assistant data

The add-on requests read-only Home Assistant API access (`homeassistant_api`). Through the Supervisor it renders one fixed template that lists the lights of the integrations in `lutron_integrations` (default `lutron_caseta` and `lutron`), with their Lutron zone ID, device serial number, name, area and current state, plus `sun.sun` sunrise and sunset and the Home Assistant time zone. Results are cached for 15 seconds. The Supervisor token never reaches the browser, and no request data is ever placed in the template.

Zones are matched to Lutron lights by Lutron zone ID first (the `zone_id` attribute that the Lutron Caséta/RA3 integration puts on each light), then by serial number (decimal, or hex such as `0x5397FB1`), then by a unique name. Matches add names, areas, live Lutron state and a link to the device in Home Assistant across the dashboard. The mapping editor suggests known serials and can fill empty zone names. The schedule chart shades night and uses the gateway show time zone for "Now" when available. Without API access the dashboard works as before.

## Proxy surface

Activity schema 2 adds nullable `command_seq`, `report_seq`, `reported`,
`reason`, and `endpoint_ts_ms` to each event, preserving the legacy fields.
On report events, legacy `requested` is a report counter and `confirmed` is
the reported level. The dashboard renders these as separate report sequence
and reported brightness values in expanded activity steps, including on older gateways. Levels are
hundredths of a percent (10000 = 100%); missing values display as “—”. Command
and report counters are independent and can wrap. Endpoint time is endpoint
uptime; the event's original `ts_ms` remains gateway uptime. A sent report
establishes local send success, not acceptance or displayed state in Lutron.
The cache and seven-day archive preserve new fields without extra polling.

- `GET /api/status`, `/api/devices`
- `GET /api/events?after=N`
- `GET /api/program?zone=N&page=N`
- `GET /api/config`, `PUT /api/config` (transactional mapping configuration)
- `GET /api/timezone`, `PUT /api/timezone` (gateway show time zone, when supported)
- `POST /api/endpoint/update` (binary firmware, maximum 786,432 bytes)
- `GET /api/ketra`, `PUT /api/ketra` (Ketra lamp inventory for direct Zigbee output, when supported)
- `GET /api/ha` (read-only Home Assistant lookup described above; served by the add-on, not the gateway)
- `GET /api/zones` (per-zone sync summary served by the add-on: Lutron state, the bridge's last command,
  the lamp's confirmed state, the current Natural Show target, and the checks behind each verdict; built
  from cached data and never adds gateway requests)

`/api/status` is forwarded as the gateway reports it, including `output_backend`,
`direct_zigbee` and `zigbee_uart` on gateways that have them (see below).

Only these methods and paths are accepted. Gateway responses are capped at 1 MiB; ordinary connections have finite timeouts, while firmware and programming transfers use a five-minute timeout. Firmware is forwarded byte-for-byte with a SHA-256 digest computed by the proxy and sent in `X-SHA256`. Query values are parsed as bounded integers. A gateway timeout or disconnect is shown as unavailable and retried by the dashboard. Event polling resumes from the latest delivered event ID, rewinds on a gateway boot ID change, and surfaces reported cursor gaps.

Configuration writes must contain exactly `generation`, `hash`, and `zones`; the gateway owns validation and transaction semantics. Each zone mapping can include `zone_id`, `name`, `n4_group`, `serial`, and `warm_dim_curve`. Keep firmware images within the endpoint's supported format and size; the add-on does not transform firmware bytes.

The add-on deliberately has no third-party Python dependencies. It lives in its own Git repository and does not replace the passive recorder app.

Run the add-on checks with `python3 -m unittest -v test_app.py` and `node test_ui.js` from this directory.

## Lighting-first monitoring (0.4.0)

A single collector reads the gateway; every browser tab reads the app cache.
Opening more tabs does not add gateway requests. The event archive uses the same
collector. New gateway firmware supplies one combined snapshot per second;
older firmware is polled serially and upgraded snapshot support is detected
within 60 seconds. Configuration refresh uses a collection slot every 30 seconds.
Timeouts back off to 30 seconds, honoring longer gateway Retry-After values.
The dashboard shows snapshot age and monitoring delays separately from actual
network disconnects. The gateway's link (Ethernet or Wi-Fi) comes from its
`network_link` status; firmware that does not report one is shown as
"Network" rather than assumed to be Wi-Fi. Existing HA Lutron labels and schedule charts are preserved.

Programming snapshots are cached until the mapping generation or boot changes.
Select **Load snapshot** to force a refresh; a busy gateway can defer inspection
so lighting commands run first. The gateway also limits management connections,
request rate, memory use and HTTP timeouts independently of the app.

Mapping writes and endpoint firmware uploads require **Enter maintenance** in
Setup. Entry warns that lighting output pauses; the newest commands are retained.
Use **End maintenance** when finished. The idle session expires after five minutes;
a bounded active transfer retains its lease until completion. Older firmware must
be updated before these maintenance operations are available. A failed output
attempt is distinguished from a superseded command or exhausted delivery retries.

The app keeps the existing read routes. Cached status, devices and configuration
responses add a `monitoring` object containing `age_s`, `delayed` and `error`.
`POST /api/maintenance` forwards exactly an `enabled` boolean. Programming reads
accept `refresh=1`. The private gateway bearer token remains server-side.

## Endpoint control and startup (0.8.0–0.8.1)

Setup has an **Endpoint control** card for the gateway's endpoint protection
(S3 gateway firmware with `/api/endpoint/power`; older firmware shows the card
as unavailable).

- **Disable endpoint** sets the gateway's operator choice to disabled. Without a
  physical power switch (the default wiring), this is a software kill switch: the
  gateway stops admitting commands and answers every endpoint HELLO with a radio
  stop. It does not cut power and relies on the endpoint firmware honouring the
  stop. Disable is offered even while monitoring is stale or backing off.
- **Enable endpoint** needs a current snapshot. The endpoint then runs its full
  startup sequence before it carries commands.
- **Clear fault latch** appears only when a current snapshot shows the endpoint
  disabled and reporting `fault-latched`, with no recovery already running. The
  gateway waits for a fresh fault report, sends one clear and leaves the endpoint
  disabled until it is enabled again. Failed clears are reported on the card.

The Overview's **Startup sequence** group shows the gateway startup hold, the
endpoint's minimum start delay (both as countdowns), the receive-only stability
window, the child attach and whether command admission is open. The endpoint
does not report the window's progress, so the panel shows a generic 5-minute
estimate from the endpoint's uptime next to the router-advert count. It starts
when listening is first seen and restarts when the endpoint reboots or its
advert count drops. Opened mid-window, it shows "Up to" because the true start
is unknown. Other restarts (partition or leader change, lost frames) are not
visible, so the endpoint can take longer than the estimate. Regating,
operator stop and fault latch are named explicitly. A stale snapshot shows no
endpoint progress. The endpoint-safety group also lists the R1/R3/interrupted
fault bits, partition, leader, routers heard and blocked transmissions.

`PUT /api/endpoint/power` forwards exactly an `enabled` boolean and
`POST /api/endpoint/safety/clear` exactly `{"confirm":"clear-r1-latch"}`; both
need `application/json` and, like maintenance, bypass the monitoring backoff.

## Activity (0.5.2)

Gateway availability/configuration events have readable labels. Endpoint
disconnects and configuration mismatches appear under **Problems only**;
recovery events are healthy. Updated gateway firmware emits these only when
state changes, while periodic heartbeats continue updating the health display.
Older gateways can still emit an activity row for each heartbeat.

The Activity tab shows one row per lighting command: what was asked, where it
got to (from Lutron → to hub → lights → to Lutron) and how it ended. Select a
row to see each step. Every step names who sent it and who received it
(Lutron → Bridge, Bridge → Ketra hub, Ketra hub → Bridge, Bridge → Lutron) and
shows only the level that step carried; its grey line holds the event number,
the technical path (Thread, UART, N4), gateway uptime and the raw `requested`
and `confirmed` fields alongside the typed report metadata. Commands are grouped
by zone and `command_seq` on schema-2 gateways (`seq` on older gateways);
otherwise each `requested` event starts a new command for its zone. Report
counters are independent and never used to correlate commands. Query replies
and other explicitly uncorrelated reports cannot complete a lighting command.
Each report names its reason and shows its reported brightness, report counter,
and endpoint uptime in the expanded details. A command ends when it is reported, dropped, undeliverable or
replaced. Reports after that are not part of it and are listed as a zone's
status updates, merged into one row until the next command. Repeated identical
N4 readings collapse into one row, and **Problems only** hides healthy rows.

The gateway has no wall clock, so the collector records the boot time
(host clock minus `gateway_uptime_ms`) and stamps each event with `wall_ms`
when it first sees it. The archive keeps `wall_ms`, and `/api/events` returns
the boot time as `boot_epoch_ms`. Times are shown in Home Assistant's time zone,
or in Pacific time (`America/Los_Angeles`) when that is unavailable.

Levels are `level_x100` (0–10000, so 5000 is 50 %). `-1` means not known yet
and is left out. Any other value is shown exactly as received with a `?` marker.

## Natural Show sensors (0.6.0)

With an MQTT broker app (such as Mosquitto) running, the app publishes six
sensors for every bridged zone (a mapped zone with a Lutron zone ID and a
serial number) through MQTT discovery. Each zone is one device, **CCX Bridge
<zone name>**:

| Sensor | Source |
|---|---|
| Active show | Show ID the endpoint has selected, or `not_selected` |
| Show target color temperature / brightness | That show evaluated now, with the endpoint's integer interpolation, in the gateway's show time zone |
| Requested color temperature / brightness | The latest command the bridge sent to N4 |
| Confirmed brightness | N4 readback |

N4 confirms brightness only. No confirmed color temperature exists, so
**Requested color temperature** is what the bridge actually sent for each show
tick. A lamp that follows its show has a requested color temperature equal to
the target, apart from the endpoint's five-second sampling and its pause while
an N4 command is in flight. A brightness override replaces the target
brightness, while the show's color temperature keeps running. A lamp that is
off sends no color, so its requested color temperature is unknown.

The Active show sensor's attributes add the selection, installed shows,
brightness override, target and requested vibrancy, requested XY, command
sequence, confirmation source and the time zone used. The gateway show time zone is read by the collector, so target values follow
its setting even when Home Assistant is configured differently. Older gateways
that do not expose a time zone use Home Assistant's time zone as before.

The selection comes from the zone's programming snapshot. While the sensors are
connected to MQTT, the collector reads one zone's snapshot per 30-second slot,
and each zone at most every two minutes, with a 20-second limit. A busy gateway
defers the read without affecting monitoring. The dashboard reuses these
snapshots. Show values become unknown when a snapshot is more than ten minutes
old. All sensors become unavailable when the gateway snapshot is more than two
minutes old or the app stops (MQTT last will). Messages are retained, and state
is published only when it changes. A zone removed from the mapping has its
sensors removed.

Turn this off with `show_sensors: false`. The app reads broker settings from
the Supervisor (`services: mqtt:want`) and still has no third-party Python
dependencies.

To flag a lamp that has drifted from its show, create a Template binary sensor
helper (Settings → Devices & services → Helpers → Template), for example:

```jinja
{% set show = states('sensor.ccx_bridge_office_active_show') %}
{% set target = states('sensor.ccx_bridge_office_show_target_color_temperature') | float(none) %}
{% set sent = states('sensor.ccx_bridge_office_requested_color_temperature') | float(none) %}
{{ show not in ['not_selected', 'unknown', 'unavailable'] and target is not none
   and sent is not none and (sent - target) | abs > 25 }}
```

Check your entity IDs; they follow the gateway's zone names.


## Gateway show time zone (0.6.1)

Setup displays the gateway's current show time zone and Home Assistant's when
they differ. Choose a supported zone and select **Apply time zone** to update
the gateway without a reboot or maintenance. The confirmation warns that show
timing and show-driven lighting can change within seconds. Zone mappings and
endpoint programming are not changed. Older gateways show that the setting is
unavailable. The collector reads the gateway setting every 30 seconds and the
show sensors and programming chart use it, including daylight-saving changes.
If a gateway reports an unrecognized legacy POSIX rule, target values remain
unknown instead of silently using a different time zone.


## Direct Zigbee output and the Zigbee member link (0.9.0)

The gateway firmware is gaining a direct Zigbee output, so it can drive the
Ketra lamps itself instead of writing to the N4 hub over WSS/TLS. The app reads
what the gateway reports in `/api/status` and never decides on its own that
the direct output is usable.

**Lighting output.** `output_backend` is `n4` or `direct_zigbee`. Gateways
that omit it write to N4, as before. With `direct_zigbee`, the output is in use
only when `direct_zigbee.output_admitted` is `true`. Current firmware keeps it
closed (`reason: production_contract_unavailable`): the gateway records each
Lutron command, holds the newest one per zone in its mailbox and sends nothing.
While output is held:

- the Overview headline reads "Lighting output held: direct Zigbee output is
  not admitted", the **Lighting output** tile shows **Held**, and the topology's
  output node is labelled **Zigbee output · held · not admitted**;
- each lamp card's middle column is **Bridge holding** with "Not sent", its
  verdict is the held output, and the Lamp column shows no reading. The
  gateway does not forward N4 observations as Zigbee readings, so the app
  never compares the lamp against old N4 values;
- the Activity tab shows the gateway's `ketra-held` step as
  "Held · direct Zigbee output not admitted". It groups with the command's
  `requested` step, appears under **Problems only**, and is not turned into
  "No progress" after two minutes;
- **Diagnostics → Direct Zigbee output (reported)** lists admission and its
  reason, the deployment, security-continuity, authenticated-readback and
  endpoint-cache-epoch readiness flags, and the receive-only diagnostic UART
  counters (HELLOs, frames, CRC errors). The HELLO role is shown as an
  unverified claim.

An unrecognised `output_backend` is shown as unknown and never as healthy.

**Zigbee member link.** A gateway built with the read-only UART2 link to a
separate Zigbee member C6 reports `zigbee_uart`. When `enabled` is `true`, the
Overview adds a **Zigbee member** tile (**Responding** when the link is
`online` and `bidirectional`) and Diagnostics adds **Zigbee member link
(reported)** with the request, response, timeout and error counters and the
member's claims: device, board, base MAC, IEEE address, build digest, boot ID,
role, state and radio. These are unauthenticated serial claims; the gateway
reports `identity_authenticated` and `rf_membership_verified` as `false`, and
the app labels them that way. A responding link does not show that the member
is on the Ketra network or that lamps are reachable. A silent link is listed as
a warning on the Overview.

**`/api/zones`** adds `output` (`backend`, `admitted`, `reason`), and each zone
adds `output`. With a held output a zone carries an `output` check, its `sent`
record has `held: true`, and `lamp` is `null`.

**MQTT.** With show sensors enabled, a **CCX Bridge Gateway** device adds two
diagnostic sensors:

| Sensor | States |
|---|---|
| Lighting output | `n4`, `n4_fallback`, `direct_zigbee_held`, `direct_zigbee` |
| Zigbee member link | `online`, `offline`, `not_enabled`, `unknown` (gateway does not report the link) |

Their attributes hold admission, its reason, the direct Zigbee readiness flags
and a subset of the member's claims and counters (no addresses). Each zone's
Active show sensor adds `output_backend` and `output_held`. While output is
held, a zone's **Requested** sensors show what Lutron asked the bridge for,
which was not sent. To be told when lighting output is held, trigger an
automation on **Lighting output** changing to `direct_zigbee_held`.


## Direct Zigbee routing and N4 fallback (0.10.0)

Gateways built for direct Zigbee routing report two more objects in
`/api/status`: `backend` (`mode` n4, direct or hybrid; the owning `phase`;
`switches`) and `direct_routing` (session readiness and its reason, node,
lease and lamp-table state, lamp counts and the reserved frame-counter range).
A **hybrid** gateway prefers direct Zigbee and falls back to N4 by itself.
While N4 carries output for it, the Lighting output tile reads **N4 fallback**
and the Overview names the direct path's reason; with direct Zigbee in
control the tile reads **Direct Zigbee**. Diagnostics shows the details under
**Direct Zigbee routing**.

**Ketra lamps** (Setup) edits the gateway's lamp inventory: for each Lutron
zone, its Ketra group (pan and logical address, or none for per-lamp sends
only) and its lamps (16-hex `eui_wire` and Ketra logical address), plus the
gateway's own Ketra address as `source`. Each lamp shows its live state from
the gateway: on/off and level, whether its Zigbee address is resolved, and
whether it is uncertain (being read back after an unconfirmed send),
unreachable, or was changed outside the bridge. **Import a lab inventory**
converts a `ketra-lab-inventory-v1` file, matching its zone names to the
mapping; unmatched zones are listed. **Apply lamps** needs maintenance mode
and sends the current generation and hash, so a concurrent edit is refused.
Gateways without the inventory show the card as not supported.

The MQTT **Lighting output** sensor reports `n4_fallback` while a hybrid
gateway is on N4, so an automation can notify you when direct Zigbee drops
out.
