#!/usr/bin/env python3
"""
tools/populate_channel.py
CLI tool for populating jellyfin_ids in Classic TV 3.0 channel YAML files.

Queries Jellyfin for all episodes of a series, validates each against the
same direct-play rules used by the Schedule_Service, and emits a ready-to-
paste YAML block (or other output format).

Also supports --append-to for adding newly imported episodes to an existing
channel file without touching any other content.

Usage:
    python3 tools/populate_channel.py --series-name "SpongeBob SquarePants"
    python3 tools/populate_channel.py --series-id 824663e6685aa3c7716463462647b9f3
    python3 tools/populate_channel.py --series-name "SpongeBob" --seasons 1-3 --output ids-only
    python3 tools/populate_channel.py --series-id <uuid> \\
        --append-to channels/nickelodeon.yaml --show-name "SpongeBob SquarePants"

Environment variables (same as Schedule_Service):
    JELLYFIN_BASE_URL   Default: http://localhost:8096
    JELLYFIN_API_KEY    Required; exits with error if absent

Requirements: 35, 36
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------
# Direct-play constants — import from server/jellyfin.py when available,
# fall back to inline definitions so the script is portable.
# ---------------------------------------------------------------------------
try:
    _repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _repo_root not in sys.path:
        sys.path.insert(0, _repo_root)
    from server.jellyfin import (  # noqa: E402
        PASSTHROUGH_AUDIO,
        PASSTHROUGH_CODECS,
        PASSTHROUGH_CONTAINERS,
    )
except ImportError:
    # Canonical source: server/jellyfin.py — keep in sync if codecs change.
    PASSTHROUGH_CONTAINERS: frozenset[str] = frozenset({"mkv", "mp4", "m4v"})  # type: ignore[misc]
    PASSTHROUGH_CODECS: frozenset[str] = frozenset({"h264", "hevc"})  # type: ignore[misc]
    PASSTHROUGH_AUDIO: frozenset[str] = frozenset({"aac", "mp3", "ac3", "eac3"})  # type: ignore[misc]

_REQUEST_TIMEOUT = 10
_LARGE_SHOW_WARNING_THRESHOLD = 500  # warn if a show exceeds this many episodes


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

def _get_env() -> tuple[str, str]:
    """Return (base_url, api_key); exit 1 if JELLYFIN_API_KEY is absent."""
    api_key = os.environ.get("JELLYFIN_API_KEY", "")
    if not api_key:
        sys.exit("Error: JELLYFIN_API_KEY environment variable is not set")
    base_url = os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096").rstrip("/")
    return base_url, api_key


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, api_key: str) -> dict[str, Any]:
    """Fetch a Jellyfin API endpoint and return parsed JSON."""
    req = urllib.request.Request(url, headers={"Authorization": f'MediaBrowser Token="{api_key}"'})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        sys.exit(f"Error: Jellyfin unreachable at {url} — {exc.reason}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"Error: Jellyfin returned HTTP {exc.code} for {url}")
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Error: unexpected error fetching {url} — {exc}")


# ---------------------------------------------------------------------------
# Series search
# ---------------------------------------------------------------------------

def _search_series(name: str, base_url: str, api_key: str) -> dict[str, Any]:
    """
    Search Jellyfin for a series by name.
    Returns the selected series dict (Id, Name, ProductionYear, etc.).
    Exits 1 on no results; prompts for selection on multiple results.
    """
    encoded = urllib.parse.quote(name)
    url = (
        f"{base_url}/Items"
        f"?searchTerm={encoded}"
        f"&IncludeItemTypes=Series"
        f"&Recursive=true"
        f"&Fields=Id,Name,ProductionYear"
        f"&Limit=20"
    )
    data = _get_json(url, api_key)
    items = data.get("Items", [])

    if not items:
        sys.exit(f"No series found matching '{name}'")

    if len(items) == 1:
        series = items[0]
        year = series.get("ProductionYear", "?")
        print(f"Found: {series['Name']} ({year})", file=sys.stderr)
        return series

    # Multiple results — prompt
    print("Multiple series found. Select one:", file=sys.stderr)
    for i, s in enumerate(items, 1):
        year = s.get("ProductionYear", "?")
        print(f"  [{i}] {s['Name']} ({year})  —  {s['Id']}", file=sys.stderr)
    while True:
        try:
            raw = input(f"Select [1-{len(items)}]: ").strip()
            choice = int(raw)
            if 1 <= choice <= len(items):
                return items[choice - 1]
        except (ValueError, EOFError):
            pass
        print(f"Please enter a number between 1 and {len(items)}", file=sys.stderr)


def _get_series_by_id(series_id: str, base_url: str, api_key: str) -> dict[str, Any]:
    """Fetch a series by its Jellyfin UUID; exits 1 if not found."""
    url = f"{base_url}/Items?Ids={series_id}&Fields=Id,Name,ProductionYear"
    data = _get_json(url, api_key)
    items = data.get("Items", [])
    if not items or not items[0].get("Id"):
        sys.exit(f"Series ID '{series_id}' not found in Jellyfin")
    return items[0]


# ---------------------------------------------------------------------------
# Episode fetch (paginated)
# ---------------------------------------------------------------------------

def _fetch_all_episodes(series_id: str, base_url: str, api_key: str) -> list[dict[str, Any]]:
    """
    Fetch all episodes for a series using paginated Jellyfin API calls.
    Includes MediaSources in each episode so validation can be done without
    additional per-episode requests.
    Uses Limit=200 per page and iterates StartIndex until all episodes retrieved.
    """
    episodes: list[dict[str, Any]] = []
    limit = 200
    start_index = 0

    while True:
        url = (
            f"{base_url}/Items"
            f"?ParentId={series_id}"
            f"&Recursive=true"
            f"&IncludeItemTypes=Episode"
            f"&SortBy=SortName"
            f"&Fields=Id,Name,ParentIndexNumber,IndexNumber,PremiereDate,MediaSources"
            f"&Limit={limit}"
            f"&StartIndex={start_index}"
        )
        data = _get_json(url, api_key)
        page_items = data.get("Items", [])
        episodes.extend(page_items)
        total = data.get("TotalRecordCount", 0)
        start_index += limit
        if start_index >= total:
            break

    return episodes


# ---------------------------------------------------------------------------
# Season filter parsing
# ---------------------------------------------------------------------------

def _parse_seasons(seasons_str: str) -> set[int]:
    """
    Parse a --seasons argument into a set of season numbers.
    Accepts "1-3" (range) or "1,3,5" (list) or "2" (single).
    Exits 1 on invalid format.
    """
    seasons: set[int] = set()
    for part in seasons_str.split(","):
        part = part.strip()
        if "-" in part:
            bounds = part.split("-", 1)
            try:
                lo, hi = int(bounds[0]), int(bounds[1])
                seasons.update(range(lo, hi + 1))
            except ValueError:
                sys.exit(f"Invalid --seasons value '{seasons_str}' — use '1-3' or '1,3,5'")
        else:
            try:
                seasons.add(int(part))
            except ValueError:
                sys.exit(f"Invalid --seasons value '{seasons_str}' — use '1-3' or '1,3,5'")
    return seasons


# ---------------------------------------------------------------------------
# Direct-play validation (per episode)
# ---------------------------------------------------------------------------

def _validate_episode(episode_id: str, base_url: str, api_key: str) -> tuple[bool, str]:
    """
    Validate whether an episode can be direct-played.
    Returns (True, "") on success; (False, reason) on failure.
    Uses the same container/codec rules as server/jellyfin.py.

    Falls back to a live API call if the episode dict doesn't have MediaSources
    (for compatibility with test mocks and future callers).
    """
    url = f"{base_url}/Items?Ids={episode_id}&Fields=MediaSources"
    req = urllib.request.Request(url, headers={"Authorization": f'MediaBrowser Token="{api_key}"'})
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, urllib.error.HTTPError, Exception):
        return False, "Jellyfin unreachable or HTTP error"

    items = data.get("Items", [])
    if not items:
        return False, "item not found in Jellyfin"
    item = items[0]
    return _validate_media_sources(item.get("MediaSources", []))


def _validate_media_sources(sources: list[dict[str, Any]]) -> tuple[bool, str]:
    """Validate a MediaSources list against direct-play codec rules."""
    for source in sources:
        container = (source.get("Container") or "").lower()
        if container not in PASSTHROUGH_CONTAINERS:
            continue
        streams = source.get("MediaStreams", [])
        video_ok = any(
            s.get("Type") == "Video" and (s.get("Codec") or "").lower() in PASSTHROUGH_CODECS
            for s in streams
        )
        audio_ok = any(
            s.get("Type") == "Audio" and (s.get("Codec") or "").lower() in PASSTHROUGH_AUDIO
            for s in streams
        )
        if video_ok and audio_ok:
            return True, ""

    # Determine the most specific failure reason
    if not sources:
        return False, "no MediaSources"
    first = sources[0]
    container = (first.get("Container") or "unknown").lower()
    if container not in PASSTHROUGH_CONTAINERS:
        return False, f"container '{container}' not in passthrough set"
    streams = first.get("MediaStreams", [])
    video_codecs = [
        (s.get("Codec") or "unknown").lower()
        for s in streams if s.get("Type") == "Video"
    ]
    if not any(c in PASSTHROUGH_CODECS for c in video_codecs):
        return False, f"video codec '{', '.join(video_codecs) or 'none'}' not in passthrough set"
    audio_codecs = [
        (s.get("Codec") or "unknown").lower()
        for s in streams if s.get("Type") == "Audio"
    ]
    return False, f"audio codec '{', '.join(audio_codecs) or 'none'}' not in passthrough set"


# ---------------------------------------------------------------------------
# Sort helpers
# ---------------------------------------------------------------------------

def _sort_key_air_date(ep: dict[str, Any]):
    return (
        ep.get("PremiereDate") or "9999",
        ep.get("ParentIndexNumber") or 9999,
        ep.get("IndexNumber") or 9999,
    )


def _sort_key_episode_number(ep: dict[str, Any]):
    return (
        ep.get("ParentIndexNumber") or 9999,
        ep.get("IndexNumber") or 9999,
    )


# ---------------------------------------------------------------------------
# Episode processing pipeline
# ---------------------------------------------------------------------------

def _process_episodes(
    episodes: list[dict[str, Any]],
    series_name: str,
    seasons_filter: set[int] | None,
    sort_by: str,
    base_url: str,
    api_key: str,
    dry_run: bool,
) -> list[dict[str, Any]]:
    """
    Apply seasons filter, sort, and direct-play validation.
    Returns a list of valid episode dicts with an injected 'series_name' key.
    Prints WARNING lines to stderr for excluded episodes.
    Exits 1 if no episodes pass validation.
    """
    # Season filter
    if seasons_filter:
        episodes = [
            ep for ep in episodes
            if ep.get("ParentIndexNumber") in seasons_filter
        ]

    # Sort
    if sort_by == "air-date":
        episodes.sort(key=_sort_key_air_date)
    elif sort_by == "episode-number":
        episodes.sort(key=_sort_key_episode_number)
    else:
        sys.exit(f"Invalid --sort-by value '{sort_by}'. Valid: air-date, episode-number")

    if dry_run:
        # In dry-run we skip validation API calls entirely
        for ep in episodes:
            ep["series_name"] = series_name
        return episodes

    valid: list[dict[str, Any]] = []
    excluded = 0

    for ep in episodes:
        # Use inline MediaSources if available (fetched with episode list);
        # fall back to a live API call only if missing.
        media_sources = ep.get("MediaSources")
        if media_sources is not None:
            ok, reason = _validate_media_sources(media_sources)
        else:
            ok, reason = _validate_episode(ep["Id"], base_url, api_key)
        s = ep.get("ParentIndexNumber", "?")
        e = ep.get("IndexNumber", "?")
        name = ep.get("Name", ep["Id"])
        if ok:
            ep["series_name"] = series_name
            valid.append(ep)
        else:
            print(
                f"WARNING: excluded S{s:02}E{e:02} '{name}' — {reason}",
                file=sys.stderr,
            )
            excluded += 1

    if not valid:
        sys.exit(
            f"No valid direct-play episodes found for '{series_name}'. "
            f"All {excluded} episode(s) failed validation."
        )

    return valid


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------

def _output_yaml_block(episodes: list[dict[str, Any]], series_name: str) -> str:
    lines = [
        f"- name: {series_name}",
        "  weight: 1",
        "  jellyfin_ids:",
    ]
    for ep in episodes:
        lines.append(f'    - "{ep["Id"]}"')
    return "\n".join(lines)


def _output_ids_only(episodes: list[dict[str, Any]]) -> str:
    return "\n".join(ep["Id"] for ep in episodes)


def _output_json(episodes: list[dict[str, Any]]) -> str:
    records = []
    for ep in episodes:
        records.append({
            "id": ep["Id"],
            "name": ep.get("Name", ""),
            "series_name": ep.get("series_name", ""),
            "season": ep.get("ParentIndexNumber"),
            "episode": ep.get("IndexNumber"),
            "air_date": ep.get("PremiereDate", "")[:10] if ep.get("PremiereDate") else None,
        })
    return json.dumps(records, indent=2)


# ---------------------------------------------------------------------------
# Summary line helper
# ---------------------------------------------------------------------------

def _season_range_str(episodes: list[dict[str, Any]]) -> str:
    seasons = sorted({ep.get("ParentIndexNumber") for ep in episodes if ep.get("ParentIndexNumber")})
    if not seasons:
        return "unknown seasons"
    if len(seasons) == 1:
        return f"season {seasons[0]}"
    if seasons == list(range(seasons[0], seasons[-1] + 1)):
        return f"seasons {seasons[0]}-{seasons[-1]}"
    return f"seasons {', '.join(str(s) for s in seasons)}"


# ---------------------------------------------------------------------------
# --append-to mode
# ---------------------------------------------------------------------------

def _run_append_to(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    """Append new episode IDs to an existing show in a channel YAML file."""
    yaml_path = os.path.abspath(args.append_to)
    show_name = args.show_name

    if not os.path.exists(yaml_path):
        sys.exit(f"File not found: {yaml_path}")

    # Import config_loader for parse/serialize
    try:
        from server.config_loader import parse, serialize
    except ImportError:
        sys.exit(
            "Error: could not import server.config_loader — "
            "run from the repository root or add it to PYTHONPATH"
        )

    with open(yaml_path, "r", encoding="utf-8") as f:
        yaml_str = f.read()

    try:
        channel = parse(yaml_str)
    except Exception as exc:  # ConfigValidationError or similar
        sys.exit(f"Invalid channel YAML: {exc}")

    # Locate the target show across all blocks
    target_show = None
    for block in channel.blocks:
        if block.shows:
            for show in block.shows:
                if show.name == show_name:
                    target_show = show
                    break
        if target_show:
            break

    if target_show is None:
        sys.exit(f"Show '{show_name}' not found in {os.path.basename(yaml_path)}")

    # Resolve series ID
    if args.series_id:
        series = _get_series_by_id(args.series_id, base_url, api_key)
    elif args.series_name:
        series = _search_series(args.series_name, base_url, api_key)
    else:
        sys.exit("Error: --append-to requires either --series-id or --series-name")

    series_id = series["Id"]
    series_name = series.get("Name", show_name)

    # Fetch all episodes
    all_episodes = _fetch_all_episodes(series_id, base_url, api_key)

    # Apply seasons filter if specified
    seasons_filter = _parse_seasons(args.seasons) if args.seasons else None
    if seasons_filter:
        all_episodes = [
            ep for ep in all_episodes
            if ep.get("ParentIndexNumber") in seasons_filter
        ]

    # Validate
    existing_ids = set(target_show.jellyfin_ids)
    new_episodes = [ep for ep in all_episodes if ep["Id"] not in existing_ids]

    if not new_episodes:
        print(f"No new episodes found for {series_name}")
        sys.exit(0)

    # Validate new episodes for direct play
    valid_new: list[dict[str, Any]] = []
    for ep in new_episodes:
        media_sources = ep.get("MediaSources")
        if media_sources is not None:
            ok, reason = _validate_media_sources(media_sources)
        else:
            ok, reason = _validate_episode(ep["Id"], base_url, api_key)
        s = ep.get("ParentIndexNumber", "?")
        e = ep.get("IndexNumber", "?")
        name = ep.get("Name", ep["Id"])
        if ok:
            valid_new.append(ep)
        else:
            print(
                f"WARNING: excluded S{s:02}E{e:02} '{name}' — {reason}",
                file=sys.stderr,
            )

    if not valid_new:
        print(f"No new valid direct-play episodes found for {series_name}")
        sys.exit(0)

    # Print diff summary
    print(f"Already present: {len(existing_ids)} episodes")
    print(f"New episodes:    {len(valid_new)} episodes")
    for ep in valid_new:
        s = ep.get("ParentIndexNumber", "?")
        e = ep.get("IndexNumber", "?")
        name = ep.get("Name", ep["Id"])
        print(f"  + S{s:02}E{e:02} - {name}")

    # Append to the show's jellyfin_ids
    target_show.jellyfin_ids.extend(ep["Id"] for ep in valid_new)

    # Warn if list is very long
    new_total = len(target_show.jellyfin_ids)
    if new_total > _LARGE_SHOW_WARNING_THRESHOLD:
        print(
            f"WARNING: show '{series_name}' now has {new_total} episodes — "
            "consider splitting into seasons",
            file=sys.stderr,
        )

    # Re-serialize and write atomically
    updated_yaml = serialize(channel)
    tmp_path = yaml_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(updated_yaml)
        os.replace(tmp_path, yaml_path)
    except OSError as exc:
        sys.exit(f"Error writing {yaml_path}: {exc}")

    print(f"Updated {yaml_path}: +{len(valid_new)} episodes added to {series_name}")


# ---------------------------------------------------------------------------
# Primary mode
# ---------------------------------------------------------------------------

def _run_primary(args: argparse.Namespace, base_url: str, api_key: str) -> None:
    """Fetch episodes for a series and emit the requested output format."""

    # Resolve series
    if args.series_id:
        series = _get_series_by_id(args.series_id, base_url, api_key)
    else:
        series = _search_series(args.series_name, base_url, api_key)

    series_id = series["Id"]
    series_name = series.get("Name", args.series_name or args.series_id)

    if args.dry_run:
        # Print search/fetch URLs only
        encoded = urllib.parse.quote(series_name)
        print(
            f"[dry-run] GET {base_url}/Items"
            f"?searchTerm={encoded}&IncludeItemTypes=Series&Recursive=true"
        )
        print(
            f"[dry-run] GET {base_url}/Items"
            f"?ParentId={series_id}&Recursive=true&IncludeItemTypes=Episode"
            f"&SortBy=SortName&Fields=Id,Name,ParentIndexNumber,IndexNumber,PremiereDate"
            f"&Limit=200&StartIndex=0"
        )

    # Fetch all episodes (even in dry-run for URL preview)
    all_episodes = _fetch_all_episodes(series_id, base_url, api_key)

    seasons_filter = _parse_seasons(args.seasons) if args.seasons else None

    valid = _process_episodes(
        all_episodes,
        series_name,
        seasons_filter,
        args.sort_by,
        base_url,
        api_key,
        dry_run=args.dry_run,
    )

    if args.dry_run:
        sys.exit(0)

    # Emit output
    output_format = args.output
    if output_format == "yaml-block":
        print(_output_yaml_block(valid, series_name))
    elif output_format == "ids-only":
        print(_output_ids_only(valid))
    elif output_format == "json":
        print(_output_json(valid))
    else:
        sys.exit(f"Invalid --output value '{output_format}'. Valid: yaml-block, ids-only, json")

    # Summary to stderr (doesn't contaminate piped output)
    season_str = _season_range_str(valid)
    excluded_total = len(all_episodes) - len(valid)
    if seasons_filter:
        season_filtered = [ep for ep in all_episodes if ep.get("ParentIndexNumber") in seasons_filter]
        excluded_total = len(season_filtered) - len(valid)

    if excluded_total > 0:
        print(
            f"{len(valid)} episodes written, {excluded_total} excluded "
            f"(validation failures) — see warnings above\n"
            f"  Series: {series_name}, {season_str}",
            file=sys.stderr,
        )
    else:
        print(
            f"{len(valid)} episodes written ({series_name}, {season_str})",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="populate_channel.py",
        description=(
            "Populate jellyfin_ids in Classic TV 3.0 channel YAML files.\n\n"
            "Queries Jellyfin for all episodes of a series, validates each for\n"
            "direct-play compatibility, and emits a ready-to-paste YAML block.\n\n"
            "Examples:\n"
            "  python3 tools/populate_channel.py --series-name 'SpongeBob SquarePants'\n"
            "  python3 tools/populate_channel.py --series-id 824663e6685aa3c7716463462647b9f3 \\\n"
            "      --append-to channels/nickelodeon.yaml --show-name 'SpongeBob SquarePants'\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--series-name",
        metavar="TEXT",
        help="Search Jellyfin for a series by name (required unless --series-id is provided)",
    )
    parser.add_argument(
        "--series-id",
        metavar="UUID",
        help="Use a known Jellyfin series UUID directly (skips the search step)",
    )
    parser.add_argument(
        "--seasons",
        metavar="RANGE",
        help="Restrict to specific seasons, e.g. '1-3' or '1,3,5'. Default: all seasons",
    )
    parser.add_argument(
        "--output",
        metavar="FORMAT",
        default="yaml-block",
        choices=["yaml-block", "ids-only", "json"],
        help="Output format: yaml-block (default), ids-only, or json",
    )
    parser.add_argument(
        "--sort-by",
        metavar="FIELD",
        default="air-date",
        choices=["air-date", "episode-number"],
        help="Sort order: air-date (default) or episode-number",
    )
    parser.add_argument(
        "--append-to",
        metavar="FILE",
        help="Append new episode IDs to a show in an existing channel YAML file",
    )
    parser.add_argument(
        "--show-name",
        metavar="NAME",
        help="Target show name within the file (required with --append-to)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print Jellyfin query URLs without making requests or writing files",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Validate argument combinations
    if not args.series_name and not args.series_id:
        parser.error("one of --series-name or --series-id is required")
    if args.append_to and not args.show_name:
        parser.error("--show-name is required when using --append-to")

    base_url, api_key = _get_env()

    if args.append_to:
        _run_append_to(args, base_url, api_key)
    else:
        _run_primary(args, base_url, api_key)


if __name__ == "__main__":
    main()
