# C6 Live Bridge Home Assistant add-on

An ingress-only dashboard for the C6 bridge gateway. The browser talks only to this add-on; the gateway bearer token remains in `/data/options.json` and is sent by the Python proxy. The proxy accepts requests only from the Home Assistant ingress peer (`172.30.32.2`) and forwards a fixed set of API routes to the configured gateway origin.

## Setup

Add `https://github.com/thetimetraveler/c6-live-bridge` through the Home Assistant app store's **Repositories** menu, then install **C6 Live Bridge**. Set `gateway_host` to the gateway's HTTP(S) origin (default `http://lutron-bridge.local:8080`, no path/query), and set `gateway_token` to its bearer token. A background poller archives events even when the dashboard is closed. The add-on writes bounded daily snapshots to `/share/c6-management/events`, includes HA receive time, and prunes files older than seven days.

The dashboard targets one-second live refreshes and shows a topology strip from Thread through the endpoint, UART, gateway and N4, with the report-return path. Flow pulses are tied to observed event stages; the diagram never infers a successful PUT or report from connectivity alone. It shows event cursor gaps, requested and confirmed endpoint values separately, gateway-uptime-based freshness (stale after 120 seconds), a read-only programming snapshot on page 0, mapping configuration, and endpoint firmware upload. Firmware uploads are capped at 786,432 bytes. No demo device data is bundled.

## Proxy surface

- `GET /api/status`, `/api/devices`
- `GET /api/events?after=N`
- `GET /api/program?zone=N&page=N`
- `GET /api/config`, `PUT /api/config` (transactional mapping configuration)
- `POST /api/endpoint/update` (binary firmware, maximum 786,432 bytes)

Only these methods and paths are accepted. Gateway responses are capped at 1 MiB; ordinary connections have finite timeouts, while firmware and programming transfers use a five-minute timeout. Firmware is forwarded byte-for-byte with a SHA-256 digest computed by the proxy and sent in `X-SHA256`. Query values are parsed as bounded integers. A gateway timeout or disconnect is shown as unavailable and retried by the dashboard. Event polling resumes from the latest delivered event ID, rewinds on a gateway boot ID change, and surfaces reported cursor gaps.

Configuration writes must contain exactly `generation`, `hash`, and `zones`; the gateway owns validation and transaction semantics. Each zone mapping can include `zone_id`, `name`, `n4_group`, `serial`, and `warm_dim_curve`. Keep firmware images within the endpoint's supported format and size; the add-on does not transform firmware bytes.

The add-on deliberately has no third-party Python dependencies. It lives in its own Git repository and does not replace the passive recorder app.

Run the add-on checks with `python3 -m unittest -v test_app.py` and `node test_ui.js` from this directory.
