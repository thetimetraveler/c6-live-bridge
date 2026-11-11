## 0.14.3

- **Zone programming is readable.** *Members* becomes **Answers to**: the Lutron IDs the lamp listens to, each labelled (this zone, another mapped zone, or an area or group), with a line explaining that a command sent to any of them reaches the lamp. *Presets* become a **Scenes** table saying what each scene does to the lamp: its brightness (converted from Lutron's 16-bit level), colour temperature or xy with a swatch, vibrancy, warm-dim curve, "Starts Natural Show …", or "Unaffected", with fade and delay in seconds. The scene last recalled is marked, and each row's raw values are in its tooltip. The summary shows the running Natural Show and how many scenes change the lamp.
- The daily schedule chart plots show levels as percentages; it showed Lutron's raw 16-bit values.

## 0.14.2

- The Overview shows no banner while everything is working; the banner appears only when something needs a look (or monitoring is delayed). The tiles still show each part's state.
- **How commands flow** is renamed **Command flow**.

## 0.14.1

- **Direct Zigbee commands are recognised in Activity and Response times.** Gateways with the native Ketra engine start a command with `native-requested`, not `requested`, so the dashboard never saw these commands start and had nothing to time. Commands now group from either start through the Ketra engine and Zigbee node steps (`native-pickup`, `selection-*`, `native-stream`, `native-queued`, `ketra-dispatch`, `ketra-uart`) to `applied`, `confirmed` and the report to Lutron. Each step has a readable name, and steps whose fields carry phases, ids or flags no longer show them as levels.
- On direct Zigbee, **Lutron → sent to lamps** is timed to `ketra-dispatch` (the frame handed to the Zigbee node) and **Lutron → lamp reading** to `applied`, which on this path means every lamp in the zone read the commanded level back. This matches the gateway's own latency benchmark.
- Gateway-side drops (`epoch-dropped`, `endpoint-safety-dropped`, `update-dropped`, `config-busy`) end a command as a problem and say why. `ketra-unrouted` reads "No Ketra lamps for this zone". Repeated lamp read requests collapse into one row.
- Programming sync's **Ketra lamps** step uses the Ketra engine's status: a failed scene sync or show start needs a look, zones that are not ready are named, and it shows lamp storage use and whether the last scene recall came from storage on the lamps or was sent as ordinary commands (and why).
- The flow diagram animates for direct Zigbee sends. **How commands flow** moves to the top of the Activity tab; its explanation is behind the **?** next to the title.

## 0.14.0

- **Direct Zigbee lamps are compared again.** 0.13.3 taught the add-on to report direct Zigbee readings, but the dashboard still discarded them ("Lamp state unavailable on this output", or "comparison incomplete") and aged them out after 2 minutes instead of 7. Direct Zigbee readings are now compared with Lutron like N4 readings.
- **In sync allows small differences**: within 2 percentage points of brightness and 10 mired of colour temperature (about 70 K at 2700 K), for both Lutron and the Natural Show. Colour is compared only when both sides report it, so a zone whose Lutron light has no colour temperature is in sync on brightness instead of "incomplete" forever.
- **Response times work on direct Zigbee.** Lamp reading and round-trip times no longer need an N4 `applied` step; labels say "sent to lamps" and "lamp reading"; N4-only figures are hidden; a **Direct Zigbee delivery** figure shows the gateway's routing counters. When nothing can be timed the card says why.
- **Reorganised.** Diagnostics is now **Health** (Components, Response times, How commands flow). The Overview keeps the headline, status tiles (each opens its details; a new **Programming** tile) and **Lights** as compact rows that open for details. A healthy bridge reads "Everything is working"; one problem reads as the problem plus what to do. Lamps waiting for a first reading no longer raise a warning; lamps whose readings stopped do.
- **Programming sync** (Programming tab): Lutron Designer → endpoint → zone mapping → the gateway's copy of each zone → scenes on the Ketra lamps, with a per-zone table. The add-on keeps the newest programming event per zone for seven days, across restarts (`/api/zones` `programming.events`), and each zone reports the gateway's latest programming read (`program`).
- **Setup** is grouped (Everyday controls, Configuration, Zigbee node and firmware) and every card says when to use it. Endpoint and Zigbee node firmware uploads sit together in **Firmware updates**, each describing which file it takes.
- Activity steps say "To lamps" and "Lamp reading" on direct Zigbee.

## 0.13.5

- Stop two false warnings in Diagnostics. Remove "Qualified for deployment" and the three rows after it (security continuity, authenticated readback, endpoint cache epoch): the gateway reports them as fixed `false` constants that say nothing about readiness. Flag **UART overflows** only while the count is still rising in normal running (after the first 2 minutes of gateway uptime, within the last 10 minutes); the burst while the Zigbee node restarts after a gateway update is shown as a total, not a warning.

## 0.13.4

- Move **Performance** and **Diagnostics** to their own **Diagnostics** tab, Performance first. Overview keeps the command flow and the Lights. The tab shows a dot when Diagnostics has something to check. On narrow screens the tab bar shortens Diagnostics and Programming to "Diag." and "Program" so all five tabs fit.

## 0.13.3

- Show direct Zigbee lamp readings. The app still assumed the gateway took no readings on direct Zigbee output, so every lamp said "Waiting for the lamp's first reading". The gateway reads each lamp after a command and every 5 minutes (every 30 s during a Natural Show); those readings are now compared with Lutron and with the bridge's last command, labelled "Lamp · Zigbee", and count as current for 7 minutes (2 minutes on N4). Held direct output still shows no reading.

## 0.13.2

- Flag lamp scene storage that stays not ready. The gateway loads Lutron scenes onto the lamps so a recall is one short message; until every lamp has been checked, scenes are sent as ordinary commands instead. If that lasts 10 minutes the attention summary says so, names the lamps not answering, and gives the time to the gateway's next check (gateway `afe69fa` and later report this in `/api/ketra/native`).

## 0.13.1

- Show Lutron as the reference for each bridged lamp. Lutron (via Home Assistant) reports the zone level the processor last commanded; lamp reports reach the processor's lamp record but never change that level. The Lights lede, the Lutron column caption, the event legend and the details of every lamp-versus-Lutron mismatch now say so, instead of implying Lutron will catch up with the lamp.
- A selected Natural Show is now described as ending on any level command, including brightness-only ones, matching a genuine A20 (endpoint `76d10f6` and later). Scene recalls with a show brightness and raise/lower still show the held show brightness.

## 0.13.0

- Add a **Performance** card to the Overview: median, p95 and maximum time from a Lutron command to hub acceptance, to the first hub reading and back to Lutron, measured on the gateway clock from the event feed; the gateway's hub API response time (`acceptance_last_ms`, `acceptance_p95_ms`, `acceptance_max_ms`) with a live sparkline; its queue-to-hub time (`last_command_ms`, `max_command_ms`); the share of hub writes accepted first time; and a waterfall of the last ten commands hop by hop. Figures are coloured against stated thresholds.
- Reorganise **Diagnostics** into five sections in command order (Thread & endpoint, Endpoint ↔ gateway link, Gateway, Lighting output, Home Assistant). Flagged values are listed first, each section header counts what needs checking, and sections with something flagged open themselves. Add the gateway values the dashboard did not show: memory low-water mark and largest block, socket use (with a meter) and failures, management clients and rejections, TLS peak, stack headroom, retries, hub WebSocket state and the last restart cause in words (crashes, watchdogs and brownouts flagged). Show the UART error rate. A missing physical hold is flagged only when a hold is configured.
- Make the **Lights** cards more visual: a glance strip with each lamp glowing in its reported colour and brightness, a single brightness bar per lamp marking the lamp, Lutron, the bridge's last command and the show target, and Lutron → bridge → lamp laid out as a flow.
- Redraw the **Natural Show** chart: the day coloured by the show's colour temperature, night shaded from Home Assistant's sunrise and sunset, axis labels, now, target, lamp and next-keyframe markers, a legend, and a hover/drag readout of the show at any time that survives the live refresh.

## 0.12.0

- Show why a gateway is not delivering lighting yet. Gateways that report `backend.hold_reason`, `admission`, `epoch_stage`, `epoch_frames_sent` and `output_owner` get five new rows in **Direct Zigbee routing (reported)**, and the Overview warns while no path owns output (for example during the gateway's boot hold, with the seconds left from `endpoint_power.boot_hold_remaining_s`), and no longer also claims that N4 is carrying lighting while it is not. Gateways that do not send them are unchanged.
- The gateway MQTT sensors' attributes add `backend_hold_reason`, `backend_output_owner` and `backend_admission`.

## 0.11.0

- Add a **Zigbee node** card in Setup for gateways that host the node's session: its state (running, resuming, not enrolled, record mismatch, error), enrollment, gateway resets, whether the reset line is wired, and the firmware transfer. **Enroll node** (maintenance) adopts the node's stored record once after the supervised USB install. **Update node firmware** sends a signed node image (up to 1.5 MB) through the gateway; the node checks it, restarts into it and keeps it only once the gateway has resumed it.
- The Overview flags a node that is not enrolled, and a node whose record does not match the gateway's journal (held until checked). Diagnostics add the node session, enrollment, resets, journal and firmware transfer.
- Proxy `POST /api/zigbee/enroll` (exactly `{}`) and `POST /api/zigbee/update` (octet-stream, at most 1,572,864 bytes, `X-SHA256` added, 10-minute timeout).
- The **Lighting output** MQTT sensor's attributes add `zigbee_node` (session state, enrollment, resets, firmware transfer).

## 0.10.0

- Support the gateway's direct Zigbee routing and automatic N4 fallback. A hybrid gateway (direct Zigbee preferred) carrying output on N4 shows **N4 fallback** on the Lighting output tile and names it in the Overview with the direct path's reason. Diagnostics adds **Direct Zigbee routing**: owning path, path switches, session readiness and its reason, node, lease and lamp-table state, lamp counts (resolved, uncertain, unreachable, changed outside the bridge), the reserved frame-counter range and send counters.
- Add a **Ketra lamps** card in Setup for the gateway's lamp inventory (`/api/ketra`): each zone's Ketra group and lamps with their live state (on/off and level, resolved address, uncertain, unreachable, changed outside the bridge), a JSON editor, and an importer that converts a `ketra-lab-inventory-v1` file by matching zone names to the mapping. Applying needs maintenance mode and the current generation and hash.
- Proxy `GET /api/ketra` (collected every 30 seconds; older gateways are marked unsupported and rechecked every five minutes) and `PUT /api/ketra` (exactly `generation`, `hash`, `source`, `zones`; at most 16 KiB).
- The **Lighting output** MQTT sensor adds `n4_fallback`, and its attributes add the backend mode, owning path, switch count and direct routing summary.

## 0.9.0

- Support gateways built with the direct Zigbee output option. The topology, Overview and Diagnostics show which output the gateway drives (`output_backend`). While the direct Zigbee output is not admitted, every Lutron command is held at the gateway: the Overview names this as the problem, the output node shows "held · not admitted", lamp cards show **Bridge holding** with "Not sent", and readings left over from N4 are never shown as the lamp's state.
- Show the new `ketra-held` activity step as "Held · direct Zigbee output not admitted", grouped with its command and listed under **Problems only**. A held command does not time out into "No progress".
- Add a **Direct Zigbee output (reported)** diagnostics group with admission, its reason, the readiness flags and the receive-only diagnostic UART counters.
- Show the read-only Zigbee member UART link (`zigbee_uart`): a **Zigbee member** tile and a diagnostics group with its counters and the member's claims (device, addresses, build digest, boot, role, state, radio). Claims are labelled as claims; a responding link is not shown as identity or RF membership.
- `/api/zones` adds `output` (backend, admitted, reason) and each zone's `output`; a zone with a held output gets an `output` check and `sent.held`.
- With show sensors enabled, add a **CCX Bridge Gateway** device with diagnostic **Lighting output** (`n4`, `direct_zigbee_held`, `direct_zigbee`) and **Zigbee member link** (`online`, `offline`, `not_enabled`, `unknown`) sensors. Each zone's Active show attributes add `output_backend` and `output_held`.
- Gateways that do not report an output backend are treated as N4, as before.

## 0.8.1

- Estimate the 5-minute stability window in the startup sequence from the endpoint's uptime, restarting it when the endpoint reboots or its router-advert count drops. A window already under way when the dashboard opens is shown as an upper bound.

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
