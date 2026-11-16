"""
Config loader for Classic TV 3.0.

Parses and validates channel YAML files. Consumed by schedule_service.py
at startup and on hot-reload.

Public API:
    load_all_channels(config_dir)  -> dict[str, Channel]
    parse(yaml_str)                -> Channel  (raises ConfigValidationError)
    serialize(channel)             -> str
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import yaml

from server.models import Block, Channel, ConfigValidationError, Show

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ClassicTV][config_loader]"

# Regex for validating the color field: "#rrggbb" lowercase hex.
_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _require(data: dict, key: str, file: str, field_path: str) -> Any:
    """Return data[key] or raise ConfigValidationError if absent."""
    if key not in data:
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"required field '{key}' is missing",
        )
    return data[key]


def _validate_str(value: Any, file: str, field_path: str, min_len: int, max_len: int) -> str:
    if not isinstance(value, str):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"must be a string, got {type(value).__name__}",
        )
    if not (min_len <= len(value) <= max_len):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"length must be {min_len}–{max_len} characters, got {len(value)}",
        )
    return value


def _validate_int(value: Any, file: str, field_path: str, min_val: int, max_val: int) -> int:
    # YAML booleans (True/False) are not valid integers here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"must be an integer, got {type(value).__name__}",
        )
    if not (min_val <= value <= max_val):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"must be between {min_val} and {max_val}, got {value}",
        )
    return value


def _validate_show(raw: Any, file: str, field_path: str) -> Show:
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"each show must be a mapping, got {type(raw).__name__}",
        )

    name_val = _require(raw, "name", file, f"{field_path}.name")
    name = _validate_str(name_val, file, f"{field_path}.name", min_len=1, max_len=64)

    weight_val = _require(raw, "weight", file, f"{field_path}.weight")
    weight = _validate_int(weight_val, file, f"{field_path}.weight", min_val=1, max_val=1000)

    ids_val = _require(raw, "jellyfin_ids", file, f"{field_path}.jellyfin_ids")
    if not isinstance(ids_val, list):
        raise ConfigValidationError(
            file=file,
            field=f"{field_path}.jellyfin_ids",
            reason=f"must be a list, got {type(ids_val).__name__}",
        )
    if len(ids_val) < 1:
        raise ConfigValidationError(
            file=file,
            field=f"{field_path}.jellyfin_ids",
            reason="must contain at least 1 Jellyfin ID",
        )
    for i, jid in enumerate(ids_val):
        if not isinstance(jid, str):
            raise ConfigValidationError(
                file=file,
                field=f"{field_path}.jellyfin_ids[{i}]",
                reason=f"each Jellyfin ID must be a string, got {type(jid).__name__}",
            )

    return Show(name=name, weight=weight, jellyfin_ids=list(ids_val))


def _validate_block(raw: Any, file: str, field_path: str) -> Block:
    if not isinstance(raw, dict):
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason=f"each block must be a mapping, got {type(raw).__name__}",
        )

    name_val = _require(raw, "name", file, f"{field_path}.name")
    name = _validate_str(name_val, file, f"{field_path}.name", min_len=1, max_len=64)

    start_val = _require(raw, "start_hour", file, f"{field_path}.start_hour")
    start_hour = _validate_int(start_val, file, f"{field_path}.start_hour", min_val=0, max_val=23)

    end_val = _require(raw, "end_hour", file, f"{field_path}.end_hour")
    end_hour = _validate_int(end_val, file, f"{field_path}.end_hour", min_val=1, max_val=24)

    # Determine offline vs shows.
    has_offline = bool(raw.get("offline", False))
    has_shows = "shows" in raw

    if has_offline and has_shows:
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason="a block cannot have both 'offline: true' and a 'shows' list",
        )
    if not has_offline and not has_shows:
        raise ConfigValidationError(
            file=file,
            field=field_path,
            reason="a block must have either 'offline: true' or a 'shows' list",
        )

    shows: list[Show] | None = None
    if has_shows:
        raw_shows = raw["shows"]
        if not isinstance(raw_shows, list):
            raise ConfigValidationError(
                file=file,
                field=f"{field_path}.shows",
                reason=f"must be a list, got {type(raw_shows).__name__}",
            )
        if not (1 <= len(raw_shows) <= 100):
            raise ConfigValidationError(
                file=file,
                field=f"{field_path}.shows",
                reason=f"must contain 1–100 shows, got {len(raw_shows)}",
            )
        shows = [
            _validate_show(s, file, f"{field_path}.shows[{i}]")
            for i, s in enumerate(raw_shows)
        ]

    return Block(
        name=name,
        start_hour=start_hour,
        end_hour=end_hour,
        shows=shows,
        offline=has_offline,
    )


def _hours_overlap(a: Block, b: Block) -> bool:
    """Return True if block a and block b have overlapping hour ranges.

    Each block covers [start_hour, end_hour) in a 24-hour day.
    A midnight-crossing block (end_hour < start_hour) wraps around.
    The range is expanded into a set of integer hours for comparison.
    """

    def block_hours(blk: Block) -> set[int]:
        if blk.end_hour > blk.start_hour:
            return set(range(blk.start_hour, blk.end_hour))
        else:
            # Midnight-crossing: e.g. start=22, end=4 → {22,23,0,1,2,3}
            return set(range(blk.start_hour, 24)) | set(range(0, blk.end_hour))

    return bool(block_hours(a) & block_hours(b))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse(yaml_str: str, *, _file: str = "<string>") -> Channel:
    """Parse a channel YAML string into a Channel dataclass.

    Raises:
        ConfigValidationError: when the YAML is structurally invalid or
            violates any field constraint.
    """
    try:
        raw = yaml.safe_load(yaml_str)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(
            file=_file,
            field="<document>",
            reason=f"YAML parse error: {exc}",
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigValidationError(
            file=_file,
            field="<document>",
            reason=f"channel YAML must be a mapping at the top level, got {type(raw).__name__}",
        )

    # --- Channel-level fields ---

    id_val = _require(raw, "id", _file, "id")
    channel_id = _validate_str(id_val, _file, "id", min_len=1, max_len=64)

    name_val = _require(raw, "name", _file, "name")
    channel_name = _validate_str(name_val, _file, "name", min_len=1, max_len=128)

    number_val = _require(raw, "number", _file, "number")
    channel_number = _validate_int(number_val, _file, "number", min_val=1, max_val=9999)

    color_val = _require(raw, "color", _file, "color")
    if not isinstance(color_val, str):
        raise ConfigValidationError(
            file=_file,
            field="color",
            reason=f"must be a string, got {type(color_val).__name__}",
        )
    if not _COLOR_RE.match(color_val):
        raise ConfigValidationError(
            file=_file,
            field="color",
            reason=(
                f"must be a 6-digit lowercase hex string prefixed with '#' "
                f"(e.g. '#a1b2c3'), got {color_val!r}"
            ),
        )

    blocks_val = _require(raw, "blocks", _file, "blocks")
    if not isinstance(blocks_val, list):
        raise ConfigValidationError(
            file=_file,
            field="blocks",
            reason=f"must be a list, got {type(blocks_val).__name__}",
        )
    if not (1 <= len(blocks_val) <= 48):
        raise ConfigValidationError(
            file=_file,
            field="blocks",
            reason=f"must contain 1–48 blocks, got {len(blocks_val)}",
        )

    blocks: list[Block] = [
        _validate_block(b, _file, f"blocks[{i}]")
        for i, b in enumerate(blocks_val)
    ]

    # --- Overlap detection (Req 5.4) ---
    for i in range(len(blocks)):
        for j in range(i + 1, len(blocks)):
            if _hours_overlap(blocks[i], blocks[j]):
                raise ConfigValidationError(
                    file=_file,
                    field="blocks",
                    reason=(
                        f"blocks[{i}] '{blocks[i].name}' and "
                        f"blocks[{j}] '{blocks[j].name}' have overlapping hour ranges"
                    ),
                )

    return Channel(
        id=channel_id,
        name=channel_name,
        number=channel_number,
        color=color_val,
        blocks=blocks,
    )


def serialize(channel: Channel) -> str:
    """Serialize a Channel dataclass to a YAML string.

    Key order: id, name, number, color, blocks.
    Block key order: name, start_hour, end_hour, then shows or offline.
    2-space indent.
    """

    def _show_to_dict(show: Show) -> dict:
        return {
            "name": show.name,
            "weight": show.weight,
            "jellyfin_ids": show.jellyfin_ids,
        }

    def _block_to_dict(block: Block) -> dict:
        d: dict = {
            "name": block.name,
            "start_hour": block.start_hour,
            "end_hour": block.end_hour,
        }
        if block.offline:
            d["offline"] = True
        else:
            d["shows"] = [_show_to_dict(s) for s in (block.shows or [])]
        return d

    doc = {
        "id": channel.id,
        "name": channel.name,
        "number": channel.number,
        "color": channel.color,
        "blocks": [_block_to_dict(b) for b in channel.blocks],
    }

    return yaml.dump(
        doc,
        default_flow_style=False,
        allow_unicode=True,
        indent=2,
        sort_keys=False,
    )


def load_all_channels(config_dir: str) -> dict[str, Channel]:
    """Load and validate all ``*.yaml`` files from *config_dir*.

    Per-file errors are logged and that channel is skipped.
    Channels sharing a channel number are excluded (both) with an ERROR log.
    Returns a dict keyed by channel ``id``.
    """
    channels: dict[str, Channel] = {}

    # Collect all *.yaml files.
    try:
        entries = os.listdir(config_dir)
    except OSError as exc:
        logger.error("%s cannot read config directory %r: %s", _LOG_PREFIX, config_dir, exc)
        return {}

    yaml_files = sorted(f for f in entries if f.endswith(".yaml"))

    for filename in yaml_files:
        filepath = os.path.join(config_dir, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                yaml_str = fh.read()
        except OSError as exc:
            logger.error(
                "%s could not read file %r: %s",
                _LOG_PREFIX, filename, exc,
            )
            continue

        try:
            channel = parse(yaml_str, _file=filename)
        except ConfigValidationError as exc:
            logger.error("%s validation error — %s", _LOG_PREFIX, exc)
            continue

        if channel.id in channels:
            logger.error(
                "%s duplicate channel id %r found in %r; skipping",
                _LOG_PREFIX, channel.id, filename,
            )
            continue

        channels[channel.id] = channel
        logger.info(
            "%s loaded channel %r (number=%d) from %r",
            _LOG_PREFIX, channel.id, channel.number, filename,
        )

    # --- Duplicate channel number check (Req 5.6) ---
    number_to_ids: dict[int, list[str]] = {}
    for ch in channels.values():
        number_to_ids.setdefault(ch.number, []).append(ch.id)

    for number, ids in number_to_ids.items():
        if len(ids) > 1:
            logger.error(
                "%s duplicate channel number %d shared by %s; excluding all",
                _LOG_PREFIX, number, ids,
            )
            for cid in ids:
                del channels[cid]

    logger.info(
        "%s finished loading: %d valid channel(s) from %r",
        _LOG_PREFIX, len(channels), config_dir,
    )

    return channels
