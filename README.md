# CCX Bridge

A Home Assistant app (add-on) for the two-board ESP32-C6 lighting bridge.
It provides a live activity diagram, requested and confirmed device state,
read-only programming inspection, mapping configuration and endpoint firmware
uploads through the gateway.

[![Add repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fthetimetraveler%2Fc6-live-bridge)

## Install

1. In Home Assistant OS, open **Settings → Apps → Install app** (older versions:
   **Settings → Add-ons → Add-on Store**).
2. Open **⋮ → Repositories**, add this URL, and select **Add**:

   ```text
   https://github.com/thetimetraveler/c6-live-bridge
   ```

3. Find **CCX Bridge** in the store and select **Install**. Home Assistant
   builds the image locally; the first installation can take a few minutes.
4. In **Configuration**, enter the gateway URL and management bearer token:

   ```yaml
   gateway_host: http://lutron-bridge.local:8080
   gateway_token: "YOUR_GATEWAY_MANAGEMENT_TOKEN"
   ```

5. Save, start the app, enable **Show in sidebar**, then select **Open Web UI**.

The gateway must already run firmware exposing the authenticated management
API on port 8080. Its management token is separate from the ESPHome native API
encryption key. Keep both out of this repository. The app supports aarch64 and
amd64 Home Assistant OS hosts.

The dashboard uses Home Assistant ingress; it does not expose a separate LAN
web port. Events are archived on the HA host for seven days with a daily size
limit. With an MQTT broker app such as Mosquitto, each bridged zone also gets
Natural Show sensors in Home Assistant: its active show, what the show calls
for now, what the bridge sent, and what N4 confirmed. On gateways with the direct Zigbee
output option, the dashboard shows which output the gateway drives, flags
commands held while that output is not admitted, and reports the read-only
Zigbee member link. A hybrid gateway prefers direct Zigbee and falls back to N4 on its own;
the dashboard shows **N4 fallback** while N4 carries lighting, and Setup's **Ketra lamps**
card edits the gateway's lamp inventory. See the [app documentation](c6-live-bridge/README.md) for API details.

## Development

The app uses Python's standard library and browser JavaScript. No dependency
installation is required for its tests:

```sh
python3 -m unittest discover -s c6-live-bridge -p 'test_*.py'
node c6-live-bridge/test_ui.js
```

Runtime credentials are entered in Home Assistant's app configuration. This
repository contains no device firmware, network secrets, or flash backups.

For an update, change the app version in `c6-live-bridge/config.yaml`, commit
and push. Select **Check for updates** in Home Assistant's app store.
