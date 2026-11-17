## 0.23.0

- Redesign the dashboard around five destinations instead of seven tabs: **Home** (is it working, what needs me), **Activity** (what just happened, and how fast), **Devices** (one card per board), **Automation** (what makes the lights change) and **Settings** (rare configuration). A left sidebar from 1000 px, a bottom tab bar below; no truncated labels. Old tab names, `#hash` links and `showTab()` calls open the new destinations.
- Home leads with one status sentence and a **Needs attention** list: each item says what is wrong, since when where known, and links to the card where it is fixed. Rooms show the last command and how fast the lamps followed it; the command path is a compact strip.
- Devices shows one card per board (gateway, Thread endpoint, Zigbee node, CCX listener, CCA radio, Ketra hub) with a status, four to six plain-language vitals, an **All readings** disclosure, its firmware update and its own controls. Flagged readings are listed above the cards.
- Show the endpoint's own health from `/api/status` (`endpoint_health`: restart reason, uptime, memory, link errors, timing) and the board-link counters (framing errors, overflows, command frames, lost and duplicate commands). A brownout, crash or watchdog restart and any lost command need attention. Older gateways show "Not reported", never zero.
- View and edit gateway rules in one place: the rule cards in Automation, and an editor in a side sheet with labelled full-width fields. Show ID 0 is accepted when the rule also gives a level, as the gateway does, so the installed touch-bar rules no longer show as errors and can be applied again.
- Settings ends with a red **Danger zone** for disabling the endpoint and clearing its safety latch; both still confirm.
- Move explanations behind an **i** control, and exact JSON, boot and cursor and inspector internals under **Advanced**. The CCA radio shows one reason instead of a grid of dashes when its status is unavailable.
- Flagged readings read as plain sentences, board first ("Thread endpoint restarted after a brownout (power dip) 31m ago"), on the board's own card and in a list of at most five above the cards; raw field names stay in All readings. Home checks and vitals use plain words.
- Home puts Needs attention directly under the status sentence; the system checks collapse into one line after the command path.
- Zone mapping rows read as text, and Edit or Add zone opens a side sheet with labelled fields. Apply mapping still sends every zone as one change.
- Show the endpoint's transmit-guard refusals and clock offset in All readings, when the gateway reports them; a refusal count adds one neutral line, never an attention item. Resent commands the gateway ignored are a reading only.
- Add design tokens (colour, type, 8-point spacing, radius, elevation) and one component set used everywhere. A light theme follows the system setting again. No proxy routes change except the CCA node update below.
- Prepare a separate CCA node firmware card for signed C6 updates through the S3. A one-time USB bootstrap is required; physical OTA and rollback qualification remain pending. The update pauses CCA while CCX and the shared event feed continue.
- Show node identity, slot, progress, failure reason and confirmation. An interrupted upload is reconciled from fresh status and is never sent again automatically. Existing Lutron end-device RF firmware controls and settings remain separate.

## 0.22.1

- Decode received CCA activity with the shared protocol decoder instead of displaying the bridge command placeholder `unknown`. Show packet names, serials, fields and remaining undecoded bytes, and search the decoded values.
- Preserve raw events and CRC status. Sensor flags remain raw where their occupied/vacant meaning is unconfirmed. This update requires no gateway or C6 firmware changes.

## 0.22.0

- Add a **Local** tab showing the gateway's own programming. Each CCX local rule reads as When → Then with Lutron names for the source keypad and target zone; disabled rules and rules that match every event are marked. The CCA node's shade feedback mappings show each shade group with its motors, and can be listed from the tab.
- Draw CCA shade targets as a window whose fabric follows the level, with Open, Close and Send level. A target whose target ID is a Lutron shade zone is detected as a shade, and its card lists the feedback motors the CCA node has mapped for it.
- Add a Light (dimmer) target drawn as a bulb with a level control. Shade and dimmer commands are built from the firmware's catalogue with the target's addresses and sent once.

## 0.21.0

- Draw CCA targets as Pico remotes. A target's layout (2-button, 2-button with raise/lower, 3-button, 3-button with raise/lower or 4-button) is detected by matching its device ID to a Pico serial in Home Assistant's Lutron data; when no Pico matches, choose the layout on the target.
- Select a Pico button to see its programming: the preset it runs, what that command sends and its JSON, with Send now, Open in command form and Edit preset. Assign or clear a button's preset there.
- Add a Pico from Lutron in one step. Preset cards show their command as readable chips and which Pico buttons run them; the JSON editor stays available. Renaming a preset's ID keeps its buttons, and removing a preset clears them.

## 0.20.0

- Show the Activity table, the CCX event inspector and the CCA packets and activity list as fixed-height live feeds, so new events no longer push the rest of the page down.
- Hold your place in a feed while you read: an expanded row, or the row you have scrolled to, stays still and keeps the scroll position of its JSON box. A bar above each feed shows how many events arrived above, with a button to jump back to the latest.
- Add Copy log to the CCX event inspector. It copies every CCX event the page holds (up to the latest 500, ignoring filters) as JSON, with the boot ID and cursor.

## 0.19.2

- Keep an expanded row in the CCX event inspector and the CCA packets and activity list in place on screen while new events arrive above it, and keep the scroll position of its JSON box.

## 0.19.1

- Keep expanded rows open in the CCX event inspector and the CCA packets and activity list when new events arrive. Rows collapse only when you close them or the gateway or node restarts.

## 0.19.0

- Add catalogue-driven shade level and feedback mapping controls when the connected CCA firmware advertises support. Motor ACKs, individual feedback transmissions and the overall feedback result appear separately from local movement completion in activity and MQTT events.
- Keep shade mappings on the CCA node. Feedback reports acknowledge a target; they do not confirm RA3 state or measure shade position. Existing CCA commands, settings and older firmware remain supported.

## 0.18.0

- Add a CCA dashboard with named targets and presets, native command forms and an advanced JSON console. Show CC1101 readiness, packet activity and correlated local command progress.
- Stage Lutron end-device firmware from LDF files or extracted bodies, verify the C6's staged prefix before resuming, and start transmission as a separate explicit action. Local transmit completion does not confirm the receiving device updated.
- Add CCA MQTT commands, results, events and a diagnostic device. Commands and events are not retained or replayed; existing CCX and Show topics continue independently.
- Label the passive B1 firmware card separately from CCA end-device firmware. Native LRF2 occupancy encoding and CCA C6 self-update remain unavailable.

## 0.17.2

- Fix the sidecar firmware card remaining on "Waiting for sidecar status" while regular gateway monitoring is active.

## 0.17.1

- Show the sidecar status reader's last polling outcome while its first status is still unavailable.

## 0.17.0

- Add a separate Sidecar firmware card for the passive B1 and signed updates through the S3 gateway. It shows the board, external antenna, running slot and full ELF hash, transfer progress and confirmation. Sidecar input pauses during an update; other bridge lighting continues.
- A shared status reader coordinates with dashboard monitoring while uploads leave it running. The CCX event reader pauses for the update and reconciliation, then resumes with a live baseline; the MQTT publisher stays running and missed events are not replayed. Uploads are never retried automatically; a lost response is reconciled through fresh status. Older gateways show sidecar updates as unsupported.

## 0.16.0

- Add a dedicated CCX live stream, generic event inspector, gateway rule editor and separately timed source/gateway stages. Existing local rule state is preserved when it is read.
- Publish new CCX events as nonretained QoS 0 MQTT messages through the existing broker service; show stream gaps and publisher drops. The publisher is independent of Show sensors and never blocks the lighting path or the app's snapshot collector.
- Drop and count MQTT events that wait over one second in the app's publish queue so a slow broker does not replay stale actions.
- Label the browser's cache-request duration correctly instead of calling it a gateway poll.

## 0.15.2

- Direct Zigbee response times require a lamp confirmation associated with that command. Periodic Natural Show readings no longer appear as roughly 30-second command latency or mark an unrelated command's lamp-reading step as complete. Corrections remain warnings.
- Routing counters identify verified zone commands, send requests including polls, and zone readbacks. Native Shows are excluded from the verified-command count; these counters do not establish a delivery success rate.
- The timing count distinguishes commands with recorded timing from requests without it. The Health page explains why Show updates may have no command confirmation.

## 0.15.1

- A lamp without a current reading listed "Hub observation unavailable" once for every passing check. It now shows one line, "No recent hub reading (or lamp reading on direct Zigbee), so nothing is compared", with how old the last reading is and how long readings count for; checks that already need a look stay listed.

## 0.15.0

- **Names from the Lutron processor.** The app reads Home Assistant's Lutron Caséta/RA3 integration diagnostics (every 10 minutes, through the Supervisor) for the processor's area tree, zones and keypads. Zone programming's **Answers to** names each ID: *Area: Second Floor › Office*, another zone, a keypad, *This lamp's device or load* (the IDs the processor numbers just after the zone), or *Group*. Lamp rows and the zone summary show the processor's area path and fixture type (for example *SpectrumTune · 1400–10000 K · warm dim*). Health → Home Assistant reports how many areas, zones and keypads were read, or why not. Without the diagnostics everything works as before.
- Scene names are not available: Home Assistant does not receive Lutron preset names, so scenes stay numbered.

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
