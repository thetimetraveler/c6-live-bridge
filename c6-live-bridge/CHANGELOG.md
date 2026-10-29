## 0.8.0

- Add an Endpoint control card in Setup: disable or enable the endpoint through the gateway's operator choice, and clear a verified fault latch behind a confirmation. Disable works while monitoring is stale and states that, without a power switch, it is a software radio stop rather than a power cut.
- Show the startup sequence on the Overview: gateway startup hold, endpoint minimum start delay, stability-window router adverts, child attach and command admission, with regating, operator stop and fault latch called out.
- List the remaining reported endpoint safety fields (R1/R3/interrupted fault bits, partition, leader, routers heard, blocked transmissions) and the gateway startup hold.
- Require fresh endpoint safety evidence before showing the endpoint as safe, keep safety evidence across snapshots, reject malformed monitoring data, and keep the original baseline of an unsaved mapping draft.

## 0.7.0

- Replace the Lights table with one card per bridged lamp. Each card shows what Lutron shows, what the bridge last sent to the Ketra hub, and what the hub reports the lamp is doing, side by side with brightness bars and colour swatches.
- Check each lamp and show a plain-language verdict: lamp against Lutron, lamp against the running Natural Show (brightness, colour and vibrancy for the current minute), and lamp against the bridge's last command. A lamp on while Lutron shows it off, as after a power cut, is flagged as out of sync.
- Show the Natural Show's target for now, its next keyframe and a curve for the day with the lamp's position.
- Render typed gateway commands (brightness, CCT, xy, vibrancy, fade) instead of the unrecognised-value marker, and explain what brightness, K and vibrancy mean.
- Add `GET /api/zones`, computed from cached data only, which also keeps zone programming fresh while the dashboard is open. The overview headline and a new "Bridged lamps" tile summarise sync.

## 0.6.1

- Read and configure the gateway show time zone in Setup, with confirmation when changing show timing.
- Evaluate show sensors and charts using the gateway's configured time zone; retain compatibility with older gateways.

## 0.6.0

- Publish per-zone Natural Show sensors through MQTT discovery: active show, show target color temperature and brightness, requested color temperature and brightness, and confirmed brightness. Targets use the endpoint's own interpolation in Home Assistant's time zone.
- Read zone programming snapshots in the background, one bounded read per 30-second slot and only while the sensors are connected, without delaying monitoring. The dashboard reuses these snapshots.
- Add the `show_sensors` option (on by default) and the optional `mqtt` service. Install the time zone database in the image.

## 0.5.3

- Rename the add-on and dashboard to CCX Bridge. The slug and repository URL are unchanged, so existing installs update in place.
- Show whether the gateway is on Ethernet or Wi-Fi from its reported `network_link`, including link up/down activity. Older firmware without a link type is labelled "Network" instead of being assumed to use Wi-Fi.

## 0.5.2

- Support gateway activity schema 2 while preserving the command timeline, sender/receiver steps, local times, and zone-ID matching.
- Separate command sequence, report counter, and reported brightness; show report reasons and endpoint timestamps in expanded steps.
- Keep property-query replies and periodic status reports separate from lighting commands, including when command sequence is zero or wraps.
- Preserve compatibility with older gateways and retain typed metadata in the cache and event archive.
