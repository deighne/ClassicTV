# Channel Management UI Spec

**Date:** September 19, 2026 (review corrections 2026-09-20)
**Status:** **Implemented** in plugin `10.11.0.9` (not yet deployed to jfmac). The review
corrections below were incorporated during implementation. Server-side validation lives in
`ChannelConfigValidator`; shuffle is in `ScheduleEngine`; the addon consumes `number` and
handles disabled-while-tuned.
**Scope:** Jellyfin dashboard config page for Classic TV channel management.

> **Detailed engineering spec:** `.kiro/specs/channel-management-ui/`
> (`requirements.md`, `design.md`, `tasks.md`). Those files carry the full acceptance
> criteria and code snippets; this document is the summary. Where they disagree with the
> corrections below, the corrections win.
>
> **Review corrections (2026-09-20)** — the spec predates the full-catalog rotation fix
> (plugin 10.11.0.7):
> 1. **Keep the schedule read endpoints `[AllowAnonymous]`.** The Kodi addon sends no token;
>    requiring auth breaks it. Only `/ClassicTV/manage/*` needs elevation.
> 2. **Shuffle must seed by rotation cycle index, not UTC calendar day**, and be applied in
>    `ScheduleEngine`, not `ScheduleCacheWorker`.
> 3. **The addon must consume `number`** (`channels.py` currently uses positional `i+1`).
> 4. **Server-side validation is required** on `POST /manage/channels` (IDs, hours, overlap,
>    duplicate numbers) — the client checks are not sufficient.
> 5. **Channel IDs should be immutable**; **disabled-while-tuned** needs a defined behaviour.
> 6. **Version numbers below are stale** — deploy as the next available version
>    (e.g. `10.11.0.9`), not `10.11.0.7`.

---

## Problem

The current setup requires a developer with SSH access and a Python script
to add or modify channels. The JSON config must be manually constructed,
episode IDs fetched via API calls, and POSTed to the plugin config endpoint.
There is no in-browser interface for any of this.

---

## Proposed interface

A Jellyfin dashboard config page (`Configuration/configPage.html` inside the
plugin) that provides full channel management without touching JSON directly.

### Page layout

```
Classic TV — Channel Management
════════════════════════════════

[ + New Channel ]

┌─ Anime (ch. 9) ──────────────────────────────── [✓ enabled] [Edit] [Delete] ┐
│ All Day (0–24h)                                                              │
│   Dragon Ball Z  ×1  linear                                                  │
│   Sailor Moon    ×1  linear                                                  │
│   Naruto         ×1  linear                                                  │
└──────────────────────────────────────────────────────────────────────────────┘

┌─ Sitcoms (ch. 4) ────────────────────────────── [✓ enabled] [Edit] [Delete] ┐
│ All Day (0–24h)                                                              │
│   Cheers        ×1  linear                                                   │
│   Seinfeld      ×1  linear                                                   │
│   Friends       ×1  linear   ...                                             │
└──────────────────────────────────────────────────────────────────────────────┘
```

Channels are sorted by channel number. Collapsed view shows block names and
show summary. Full edit opens a modal or expands inline.

---

## Channel editor (modal or inline expand)

```
Channel Name:  [ Sitcoms             ]
Channel ID:    [ sitcoms             ]  (slug, auto-generated from name, editable)
Channel #:     [ 4                   ]  (determines sort order and digit entry)
Enabled:       [✓]
Start Anchor:  [ 2024-01-01T00:00:00Z ]

Blocks:
  [ + Add Block ]

  ┌─ All Day ─────────────────────────────── [Edit] [Delete] ┐
  │ Hours: 0 – 24                                            │
  │ Offline: [ ]                                             │
  │                                                          │
  │ Shows:  [ + Add Show ]                                   │
  │                                                          │
  │  Cheers         Weight: [1]  Order: [Linear ▾]  [Remove] │
  │  Seinfeld       Weight: [1]  Order: [Linear ▾]  [Remove] │
  │  Friends        Weight: [1]  Order: [Shuffle▾]  [Remove] │
  │  The Office     Weight: [1]  Order: [Linear ▾]  [Remove] │
  └──────────────────────────────────────────────────────────┘

[ Save ]  [ Cancel ]
```

---

## Data model changes (server plugin)

Three new fields on existing types. All are backward-compatible defaults.

### `ChannelConfig` — add `number` and `enabled`

```csharp
/// <summary>Channel number for digit entry and sort order. 0 = unassigned.</summary>
[JsonPropertyName("number")]
public int Number { get; set; } = 0;

/// <summary>When false, channel is hidden from the schedule API and Kodi addon.</summary>
[JsonPropertyName("enabled")]
public bool Enabled { get; set; } = true;
```

`GET /ClassicTV/channels` filters out disabled channels. `GET /ClassicTV/channel/{id}`
returns 404 for disabled channels. `ScheduleCacheWorker` skips disabled channels.

### `Show` — add `shuffle`

```csharp
/// <summary>
/// When true, episodes are played in a no-repeat shuffle order instead of
/// sequentially. The WRR rotation still applies across shows; shuffle affects
/// only the episode chosen within a show's turn.
/// </summary>
[JsonPropertyName("shuffle")]
public bool Shuffle { get; set; } = false;
```

**Shuffle implementation — no-repeat Fisher-Yates in `ScheduleEngine`:**

Jellyfin's built-in shuffle infrastructure (`PlayQueueManager`, `GroupShuffleMode`)
is for SyncPlay/group playback and is not accessible from a plugin's schedule engine.
We implement no-repeat shuffle directly.

When `Shuffle = true` for a show, `ExpandBlockEpisodes` produces a shuffled episode
order instead of the sequential cursor walk. The shuffle is:

1. **Deterministic per rotation cycle** — seeded from a hash of `channel.Id + block.Name +
   show.Name + cycleIndex`, where `cycleIndex = floor(elapsed / cycleDuration)`. The cycle
   always contains the same episodes, so its duration is stable and `cycleIndex` is
   well-defined and reproducible across restarts. **Do not seed by calendar date** — a
   midnight reseed would change the permutation mid-rotation, duplicating or skipping
   episodes and causing a visible schedule jump.

2. **No-repeat** — Fisher-Yates shuffle of the full `JellyfinIds` list before the WRR
   slot expansion. Every episode appears exactly once before any repeats.

3. **Cycle reset** — when `cycleIndex` increments, a new seed produces a new permutation
   for the next full rotation. No explicit "re-shuffle on exhaustion" step is needed; it
   falls out of the per-cycle seed.

Concrete implementation in `ExpandBlockEpisodes`:

```csharp
// For each show with Shuffle = true, before cursor iteration:
var ids = show.Shuffle
    ? FisherYatesShuffle(show.JellyfinIds, SeedFor(channel, block, show))
    : show.JellyfinIds;

// Then: ep_idx = cursors[best] % ids.Count (unchanged)

private static List<string> FisherYatesShuffle(List<string> items, int seed)
{
    var result = new List<string>(items);
    var rng = new Random(seed);
    for (int i = result.Count - 1; i > 0; i--)
    {
        int j = rng.Next(i + 1);
        (result[i], result[j]) = (result[j], result[i]);
    }
    return result;
}

private static int SeedFor(ChannelConfig channel, Block block, Show show, long cycleIndex)
{
    // Deterministic per rotation cycle — same order for the whole cycle,
    // new permutation when cycleIndex increments.
    return HashCode.Combine(channel.Id, block.Name, show.Name, cycleIndex);
}
```

**Corrected location:** apply shuffle **inside `ScheduleEngine.ComputeSchedule`**, which
already computes `elapsed` and `cycleDuration` (and therefore `cycleIndex`). The earlier
recommendation to pre-shuffle in `ScheduleCacheWorker` is wrong: the worker builds a
48-hour window that can cross a cycle boundary, so a single per-build permutation cannot
be correct. The engine remains pure and deterministic; it may read the `shuffle` flag.

---

## New API endpoints (server plugin)

The config page calls the Jellyfin plugin's own management endpoints.
These are in addition to the existing read-only schedule endpoints.

### `GET /ClassicTV/manage/channels`
Returns the full channel list including disabled channels, with all config fields.
For the config page only — the Kodi addon uses `/ClassicTV/channels` (filtered).

### `POST /ClassicTV/manage/channels`
Replace the full channel config. Body: `ChannelConfigRoot` JSON.
Returns 204 on success. Triggers `RefreshDurationsTask` and cache rebuild.

### `GET /ClassicTV/manage/library/series`
Returns all TV series in the Jellyfin library, sorted by name.
Response: `[{ "id": "...", "name": "...", "episodeCount": N }, ...]`
Used by the "Add Show" picker in the channel editor.

### `GET /ClassicTV/manage/library/series/{seriesId}/episodes`
Returns all episodes for a series, sorted by season/episode.
Response: `[{ "id": "...", "name": "...", "season": N, "episode": N }, ...]`
Used to preview what will be added when a show is selected.

---

## Config page implementation

### Technology

Standard Jellyfin dashboard config page: a single `configPage.html` file in the
plugin's `Configuration/` directory, declared in `plugin.xml` / `Plugin.cs` via
`IHasWebPages`. Uses vanilla JS + the Jellyfin dashboard's built-in API client.
No React, no build step, no external dependencies.

### File

`Jellyfin.Plugin.ClassicTV/Configuration/configPage.html`

Declared in `Plugin.cs`:
```csharp
public override IEnumerable<PluginPageInfo> GetPages()
{
    yield return new PluginPageInfo
    {
        Name = "ClassicTV",
        EmbeddedResourcePath = $"{GetType().Namespace}.Configuration.configPage.html"
    };
}
```

Plugin class changes from `BasePlugin<PluginConfiguration>` — it already implements
this. Just needs `IHasWebPages` added to the interface list.

### Page structure

```html
<!-- Configuration/configPage.html -->
<div id="ClassicTVConfigPage" data-role="page" class="page">
  <div data-role="content">
    <div class="content-primary">
      <h2>Classic TV — Channels</h2>
      <button id="btnNewChannel">+ New Channel</button>
      <div id="channelList"><!-- rendered by JS --></div>
    </div>
  </div>
</div>
```

### JS flow

1. On page load: `GET /ClassicTV/manage/channels` → render channel cards
2. "New Channel" → open editor modal with blank form
3. "Edit" on a channel → open editor modal pre-filled with channel data
4. "Delete" → confirm dialog → remove from local state → save
5. "Enable/Disable" toggle → flip `enabled` → save
6. "Add Show" in block editor → open series picker modal
   - `GET /ClassicTV/manage/library/series` → searchable list
   - On select: `GET /ClassicTV/manage/library/series/{id}/episodes` → show count
   - On confirm: add show to block with default weight=1, shuffle=false
7. "Save" (channel editor) → validate → update local channel list → `POST /ClassicTV/manage/channels`

### Validation

Client-side before POST:
- Channel ID: non-empty, alphanumeric + hyphens, unique across channels
- Channel number: integer ≥ 0, unique across enabled channels (warn on duplicate, don't block)
- Block hours: startHour 0–23, endHour 1–24, endHour > startHour (for normal blocks)
- Show: at least one episode ID (i.e. series exists in library and was selected)
- Weight: integer 1–100

---

## Drag-and-drop ordering (v2 feature, not in this spec)

Reordering shows within a block and reordering blocks within a channel via drag-and-drop
would be a useful UX improvement but is not in scope for this spec. The initial version
uses "move up / move down" buttons or simple list ordering with explicit controls.

---

## `startAnchor` in the UI

`startAnchor` is an advanced field. Most users should not need to touch it —
the default (`2024-01-01T00:00:00Z`) is fine for all channels. Expose it as
a collapsed "Advanced" section within the channel editor, with a note:
"This is the date from which the channel's rotation is calculated. Change it
to reset what episode plays first."

---

## Implementation order

The server plugin and UI are tightly coupled here. Recommended sequence:

1. **Add model fields** (`number`, `enabled`, `shuffle`) to `Models.cs` — backward compat, tests first
2. **Update `ScheduleEngine`** for shuffle support + tests
3. **Update `ScheduleCacheWorker`** to skip disabled channels
4. **Update `GET /ClassicTV/channels`** to filter disabled, include `number`
5. **Add management endpoints** (`/manage/channels`, `/manage/library/series`, `/manage/library/series/{id}/episodes`)
6. **Add `IHasWebPages` to `Plugin.cs`**
7. **Build `configPage.html`** — channel list, editor modal, series picker
8. **Add server-side validation** to `POST /manage/channels` (IDs, hours, overlap, duplicate numbers)
9. **Update the Kodi addon** to consume the server `number` (`schedule.py` + `channels.py`, with `tests4` coverage)
10. **Deploy** as the next available version (e.g. `10.11.0.9`) + update `channels-v4.json` with `number` and `enabled` fields

---

## Open questions — RESOLVED

1. **Authentication on management endpoints:** `[Authorize(Policy = Policies.RequiresElevation)]`
   — admin auth required. The config page is accessed via Dashboard → Plugins → ClassicTV,
   which requires an authenticated admin session. Management endpoints must not be callable
   unauthenticated.

2. **Series search UX:** Client-side filter on a pre-loaded series list. The list loads
   once on page open; filtering is instant. No server-side search needed given library sizes.

3. **Episode ordering for shuffle:** Always store `jellyfinIds` in season/episode order.
   Shuffle is applied at cache-build time in `ScheduleCacheWorker`, not stored in config.
   Switching a show from shuffle back to linear always gives a predictable sequential order.

4. **Block hours display:** Label as "Chicago time (CT)" with a tooltip note about DST.

5. **`startAnchor` reset:** Provide a "Reset to now" button that sets `startAnchor` to
   the current UTC time (ISO 8601). Also provide a "Reset to Jan 1 2024" button as the
   standard project anchor.
