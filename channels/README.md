# channels/

> **v3 legacy.** The live 4.0 system does not read these YAML files. The Jellyfin plugin's
> channel config is the JSON in `channels-v4.json` (stored in the plugin config as
> `ChannelsJson`), and schedule data is served at `http://jfmac:8096/ClassicTV/...`. This
> file describes the decommissioned v3 Schedule_Service format and is kept for reference.

One YAML file per channel. The Schedule_Service loads all `*.yaml` files in this directory at startup and hot-reloads within 5 seconds of any change.

## File format

See `requirements.md` Requirement 5 for the full channel YAML schema.

## Populating jellyfin_ids

Each `show` entry in a channel YAML requires individual Jellyfin episode UUIDs in `jellyfin_ids` — one per episode, in playback order. Use `tools/populate_channel.py` to generate these from your Jellyfin library.

**Initial population** — fetch all episodes for a series and emit a paste-ready YAML block:

```bash
# By series name (prompts if multiple matches)
JELLYFIN_API_KEY=your-key python3 tools/populate_channel.py \
    --series-name "SpongeBob SquarePants"

# By series UUID (no prompt)
JELLYFIN_API_KEY=your-key python3 tools/populate_channel.py \
    --series-id 824663e6685aa3c7716463462647b9f3

# Restrict to specific seasons
JELLYFIN_API_KEY=your-key python3 tools/populate_channel.py \
    --series-name "SpongeBob SquarePants" --seasons 1-3
```

Pipe the output directly or copy-paste the `- name: ...` block into your channel YAML under the appropriate `block.shows` list.

**Ongoing maintenance** — after importing new episodes into Jellyfin, append only the new UUIDs to an existing show without touching anything else:

```bash
JELLYFIN_API_KEY=your-key python3 tools/populate_channel.py \
    --series-id 824663e6685aa3c7716463462647b9f3 \
    --append-to channels/nickelodeon.yaml \
    --show-name "SpongeBob SquarePants"
```

The tool validates every episode against the same direct-play rules as the Schedule_Service before including it. Episodes that fail validation (wrong container or codec) are printed as warnings and excluded from output.

Run `python3 tools/populate_channel.py --help` for full usage.
