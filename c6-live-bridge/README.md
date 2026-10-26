# C6 Live Bridge Home Assistant add-on

An ingress-only dashboard for the C6 bridge gateway. The browser talks only to this add-on; the gateway bearer token remains in `/data/options.json` and is sent by the Python proxy. The proxy accepts requests only from the Home Assistant ingress peer (`172.30.32.2`) and forwards a fixed set of API routes to the configured gateway origin.

## Setup

Add `https://github.com/thetimetraveler/c6-live-bridge` through the Home Assistant app store's **Repositories** menu, then install **C6 Live Bridge**. Set `gateway_host` to the gateway's HTTP(S) origin (default `http://lutron-bridge.local:8080`, no path/query), and set `gateway_token` to its bearer token. A background poller archives events even when the dashboard is closed. The add-on writes bounded daily snapshots to `/share/c6-management/events`, includes HA receive time, and prunes files older than seven days.

The dashboard targets one-second live refreshes and shows a topology strip from Thread through the endpoint, UART, gateway and N4, with the report-return path. Flow pulses are tied to observed event stages; the diagram never infers a successful PUT or report from connectivity alone. It shows event cursor gaps, requested and confirmed endpoint values separately, gateway-uptime-based freshness (stale after 120 seconds), a read-only programming snapshot on page 0, mapping configuration, and endpoint firmware upload. Firmware uploads are capped at 786,432 bytes. No demo device data is bundled.

## Home Assistant data

The add-on requests read-only Home Assistant API access (`homeassistant_api`). Through the Supervisor it renders one fixed template that lists the lights of the integrations in `lutron_integrations` (default `lutron_caseta` and `lutron`), with their Lutron zone ID, device serial number, name, area and current state, plus `sun.sun` sunrise and sunset and the Home Assistant time zone. Results are cached for 15 seconds. The Supervisor token never reaches the browser, and no request data is ever placed in the template.

Zones are matched to Lutron lights by Lutron zone ID first (the `zone_id` attribute that the Lutron Caséta/RA3 integration puts on each light), then by serial number (decimal, or hex such as `0x5397FB1`), then by a unique name. Matches add names, areas, live Lutron state and a link to the device in Home Assistant across the dashboard. The mapping editor suggests known serials and can fill empty zone names. The schedule chart shades night and uses Home Assistant's time zone for "Now". Without API access the dashboard works as before.

## Proxy surface

- `GET /api/status`, `/api/devices`
- `GET /api/events?after=N`
- `GET /api/program?zone=N&page=N`
- `GET /api/config`, `PUT /api/config` (transactional mapping configuration)
- `POST /api/endpoint/update` (binary firmware, maximum 786,432 bytes)
- `GET /api/ha` (read-only Home Assistant lookup described above; served by the add-on, not the gateway)

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
Wi-Fi disconnects. Existing HA Lutron labels and schedule charts are preserved.

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

## Activity (0.5.0)

The Activity tab shows one row per lighting command: what was asked, where it
got to (sent → hub → lights → Lutron) and how it ended. Select a row to see each
step with its event number and gateway uptime. Commands are grouped by zone and
command `seq` when the gateway reports one; otherwise each `requested` event
starts a new command for its zone. Repeated identical N4 readings collapse into
one row, and **Problems only** hides healthy rows.

The gateway has no wall clock, so the collector records the boot time
(host clock minus `gateway_uptime_ms`) and stamps each event with `wall_ms`
when it first sees it. The archive keeps `wall_ms`, and `/api/events` returns
the boot time as `boot_epoch_ms`. Times are shown in Home Assistant's time zone,
or in Pacific time (`America/Los_Angeles`) when that is unavailable.

Levels are `level_x100` (0–10000, so 5000 is 50 %). Any other value, including
`-1`, is shown exactly as received with a `?` marker.
