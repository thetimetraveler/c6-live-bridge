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
