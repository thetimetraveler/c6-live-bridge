## 0.5.3

- Rename the add-on and dashboard to CCX Bridge. The slug and repository URL are unchanged, so existing installs update in place.

## 0.5.2

- Support gateway activity schema 2 while preserving the command timeline, sender/receiver steps, local times, and zone-ID matching.
- Separate command sequence, report counter, and reported brightness; show report reasons and endpoint timestamps in expanded steps.
- Keep property-query replies and periodic status reports separate from lighting commands, including when command sequence is zero or wraps.
- Preserve compatibility with older gateways and retain typed metadata in the cache and event archive.
