"""
Unit tests for server/config_loader.py.

Tests Requirements: 34.1, 34.2, 34.3, 34.4, 34.5

Coverage:
- Valid YAML round-trips (parse, serialize, idempotent serialize)
- Every validation error type raises ConfigValidationError
- Overlapping block detection (normal, non-overlapping, midnight-crossing)
- Duplicate channel number exclusion in load_all_channels
- Malformed YAML raises ConfigValidationError
- All errors raise ConfigValidationError, never KeyError/TypeError/AttributeError
"""

import textwrap

import pytest
import yaml

from server.config_loader import load_all_channels, parse, serialize
from server.models import Block, Channel, ConfigValidationError, Show


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_VALID_YAML = textwrap.dedent("""\
    id: test-channel
    name: Test Channel
    number: 42
    color: "#ff6600"
    blocks:
      - name: Morning
        start_hour: 6
        end_hour: 12
        shows:
          - name: Test Show
            weight: 1
            jellyfin_ids:
              - abc123
""")


def _make_channel_yaml(
    id: str = "test-channel",
    name: str = "Test Channel",
    number: int = 42,
    color: str = "#ff6600",
    blocks_yaml: str | None = None,
) -> str:
    """Build a channel YAML string with sensible defaults."""
    if blocks_yaml is None:
        blocks_yaml = textwrap.dedent("""\
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Test Show
                    weight: 1
                    jellyfin_ids:
                      - abc123
        """)
    return textwrap.dedent(f"""\
        id: {id!r}
        name: {name!r}
        number: {number}
        color: {color!r}
    """) + blocks_yaml


def _write_yaml(tmp_path, filename: str, content: str) -> None:
    """Write a YAML file to tmp_path."""
    (tmp_path / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Valid YAML round-trips
# ---------------------------------------------------------------------------


class TestValidRoundTrips:
    """Tests for valid channel parsing and serialization."""

    def test_complete_channel_two_blocks(self):
        """A channel with one normal block and one offline block parses correctly."""
        yaml_str = textwrap.dedent("""\
            id: classic-nick
            name: Classic Nickelodeon
            number: 5
            color: "#ff6600"
            blocks:
              - name: Primetime
                start_hour: 8
                end_hour: 22
                shows:
                  - name: Hey Arnold
                    weight: 3
                    jellyfin_ids:
                      - id-111
                      - id-222
                  - name: Rugrats
                    weight: 1
                    jellyfin_ids:
                      - id-333
              - name: Overnight
                start_hour: 22
                end_hour: 8
                offline: true
        """)
        channel = parse(yaml_str)

        assert channel.id == "classic-nick"
        assert channel.name == "Classic Nickelodeon"
        assert channel.number == 5
        assert channel.color == "#ff6600"
        assert len(channel.blocks) == 2

        primetime = channel.blocks[0]
        assert primetime.name == "Primetime"
        assert primetime.start_hour == 8
        assert primetime.end_hour == 22
        assert primetime.offline is False
        assert len(primetime.shows) == 2

        hey_arnold = primetime.shows[0]
        assert hey_arnold.name == "Hey Arnold"
        assert hey_arnold.weight == 3
        assert hey_arnold.jellyfin_ids == ["id-111", "id-222"]

        rugrats = primetime.shows[1]
        assert rugrats.name == "Rugrats"
        assert rugrats.weight == 1
        assert rugrats.jellyfin_ids == ["id-333"]

        overnight = channel.blocks[1]
        assert overnight.name == "Overnight"
        assert overnight.start_hour == 22
        assert overnight.end_hour == 8
        assert overnight.offline is True
        assert overnight.shows is None

    def test_serialize_produces_valid_yaml(self):
        """serialize(channel) produces YAML that round-trips to identical field values."""
        channel = parse(MINIMAL_VALID_YAML)
        serialized = serialize(channel)

        # Must be parseable YAML
        raw = yaml.safe_load(serialized)
        assert raw is not None

        # Round-trip back to a Channel
        channel2 = parse(serialized)
        assert channel2.id == channel.id
        assert channel2.name == channel.name
        assert channel2.number == channel.number
        assert channel2.color == channel.color
        assert len(channel2.blocks) == len(channel.blocks)

        b1 = channel.blocks[0]
        b2 = channel2.blocks[0]
        assert b2.name == b1.name
        assert b2.start_hour == b1.start_hour
        assert b2.end_hour == b1.end_hour
        assert b2.offline == b1.offline

        if b1.shows:
            assert len(b2.shows) == len(b1.shows)
            s1, s2 = b1.shows[0], b2.shows[0]
            assert s2.name == s1.name
            assert s2.weight == s1.weight
            assert s2.jellyfin_ids == s1.jellyfin_ids

    def test_serialize_idempotent(self):
        """serialize(parse(serialize(channel))) == serialize(channel)."""
        channel = parse(MINIMAL_VALID_YAML)
        once = serialize(channel)
        twice = serialize(parse(once))
        assert once == twice

    def test_serialize_key_order(self):
        """serialize() produces keys in specified order: id, name, number, color, blocks."""
        channel = parse(MINIMAL_VALID_YAML)
        raw = yaml.safe_load(serialize(channel))
        keys = list(raw.keys())
        assert keys == ["id", "name", "number", "color", "blocks"]

    def test_serialize_block_key_order(self):
        """Block keys appear in order: name, start_hour, end_hour, then shows/offline."""
        channel = parse(MINIMAL_VALID_YAML)
        raw = yaml.safe_load(serialize(channel))
        block_keys = list(raw["blocks"][0].keys())
        assert block_keys[:3] == ["name", "start_hour", "end_hour"]
        assert "shows" in block_keys or "offline" in block_keys

    def test_serialize_offline_block_no_shows_key(self):
        """Offline blocks should serialize with 'offline: true', no 'shows' key."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Night
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        channel = parse(yaml_str)
        raw = yaml.safe_load(serialize(channel))
        block = raw["blocks"][0]
        assert block.get("offline") is True
        assert "shows" not in block

    def test_parse_minimal_valid(self):
        """The minimal valid YAML parses without error."""
        channel = parse(MINIMAL_VALID_YAML)
        assert channel.id == "test-channel"
        assert channel.number == 42

    def test_boundary_values_accepted(self):
        """Boundary values at min/max range limits are accepted."""
        # id: 1 char, name: 1 char, number: 1, color valid, 1 block, weight 1, 1 jellyfin_id
        yaml_str = textwrap.dedent("""\
            id: "a"
            name: "B"
            number: 1
            color: "#000000"
            blocks:
              - name: "X"
                start_hour: 0
                end_hour: 1
                shows:
                  - name: "Y"
                    weight: 1
                    jellyfin_ids:
                      - id1
        """)
        channel = parse(yaml_str)
        assert channel.id == "a"
        assert channel.number == 1

    def test_max_boundary_values_accepted(self):
        """Max boundary values are accepted."""
        yaml_str = textwrap.dedent(f"""\
            id: {"x" * 64!r}
            name: {"y" * 128!r}
            number: 9999
            color: "#ffffff"
            blocks:
              - name: {"z" * 64!r}
                start_hour: 23
                end_hour: 24
                shows:
                  - name: "Show"
                    weight: 1000
                    jellyfin_ids:
                      - id1
        """)
        channel = parse(yaml_str)
        assert channel.number == 9999
        assert channel.blocks[0].shows[0].weight == 1000


# ---------------------------------------------------------------------------
# 2. Validation errors — channel-level fields
# ---------------------------------------------------------------------------


class TestMissingChannelFields:
    """Tests for missing required channel-level fields."""

    def test_missing_id(self):
        yaml_str = textwrap.dedent("""\
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "id" in str(exc_info.value)

    def test_missing_name(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            number: 42
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "name" in str(exc_info.value)

    def test_missing_number(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "number" in str(exc_info.value)

    def test_missing_color(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_missing_blocks(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "blocks" in str(exc_info.value)


class TestIdValidation:
    def test_id_too_long(self):
        long_id = "x" * 65
        yaml_str = _make_channel_yaml(id=long_id)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "id" in str(exc_info.value)

    def test_id_exactly_64_chars_ok(self):
        yaml_str = _make_channel_yaml(id="x" * 64)
        channel = parse(yaml_str)
        assert len(channel.id) == 64

    def test_id_not_string(self):
        yaml_str = textwrap.dedent("""\
            id: 12345
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        # YAML parses 12345 as int — should fail type check
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "id" in str(exc_info.value)


class TestNameValidation:
    def test_name_too_long(self):
        yaml_str = _make_channel_yaml(name="y" * 129)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "name" in str(exc_info.value)

    def test_name_exactly_128_chars_ok(self):
        yaml_str = _make_channel_yaml(name="y" * 128)
        channel = parse(yaml_str)
        assert len(channel.name) == 128


class TestNumberValidation:
    def test_number_zero(self):
        yaml_str = _make_channel_yaml(number=0)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "number" in str(exc_info.value)

    def test_number_10000(self):
        yaml_str = _make_channel_yaml(number=10000)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "number" in str(exc_info.value)

    def test_number_negative(self):
        yaml_str = _make_channel_yaml(number=-1)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "number" in str(exc_info.value)

    def test_number_as_float_rejected(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42.5
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "number" in str(exc_info.value)


class TestColorValidation:
    def test_color_not_string(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: 123456
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_uppercase_hex(self):
        yaml_str = _make_channel_yaml(color="#FF6600")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_missing_hash(self):
        yaml_str = _make_channel_yaml(color="ff6600")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_five_digit_hex(self):
        yaml_str = _make_channel_yaml(color="#ff660")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_seven_digits(self):
        yaml_str = _make_channel_yaml(color="#ff66001")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_with_invalid_chars(self):
        yaml_str = _make_channel_yaml(color="#gg6600")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)

    def test_color_empty_string(self):
        yaml_str = _make_channel_yaml(color="")
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "color" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. Validation errors — blocks list
# ---------------------------------------------------------------------------


class TestBlocksListValidation:
    def test_blocks_empty_list(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks: []
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "blocks" in str(exc_info.value)

    def test_blocks_not_list(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks: "not a list"
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "blocks" in str(exc_info.value)

    def test_blocks_49_items(self):
        """49 blocks should fail (max is 48)."""
        blocks_entries = []
        # 49 non-overlapping single-hour blocks — but 24 hours only goes 0-23 start.
        # Use offline blocks and spread them 1 hour each, but we can't fit 49 in 24 hours.
        # Instead we test the count limit separately from overlap.
        # Create 49 offline single-hour blocks that DON'T overlap — but 24 hours can't fit 49.
        # So just put them all as hour 0-1 and rely on count check happening before overlap check,
        # or use a raw dict approach. Actually, the parser checks count before iterating blocks.
        # We rely on the count being checked first.
        for i in range(49):
            blocks_entries.append(
                f"  - name: Block{i}\n    start_hour: 0\n    end_hour: 1\n    offline: true"
            )
        blocks_yaml = "blocks:\n" + "\n".join(blocks_entries) + "\n"
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
        """) + blocks_yaml
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "blocks" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 4. Validation errors — individual block fields
# ---------------------------------------------------------------------------


class TestBlockFieldValidation:
    """Tests for individual block field validation."""

    def _yaml_with_block(self, block_yaml: str) -> str:
        return textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks:
        """) + textwrap.indent(block_yaml, "  ")

    def test_block_missing_name(self):
        block_yaml = textwrap.dedent("""\
            - start_hour: 6
              end_hour: 12
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "name" in str(exc_info.value)

    def test_block_start_hour_negative(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: -1
              end_hour: 6
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "start_hour" in str(exc_info.value)

    def test_block_start_hour_24(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 24
              end_hour: 6
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "start_hour" in str(exc_info.value)

    def test_block_end_hour_zero(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 0
              end_hour: 0
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "end_hour" in str(exc_info.value)

    def test_block_end_hour_25(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 0
              end_hour: 25
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "end_hour" in str(exc_info.value)

    def test_block_both_offline_and_shows(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 6
              end_hour: 12
              offline: true
              shows:
                - name: Show
                  weight: 1
                  jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "offline" in str(exc_info.value) or "shows" in str(exc_info.value)

    def test_block_neither_offline_nor_shows(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 6
              end_hour: 12
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        # Error message should mention offline or shows
        assert "offline" in str(exc_info.value) or "shows" in str(exc_info.value)

    def test_block_name_too_long(self):
        block_yaml = textwrap.dedent(f"""\
            - name: {"x" * 65!r}
              start_hour: 6
              end_hour: 12
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "name" in str(exc_info.value)

    def test_block_missing_start_hour(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              end_hour: 12
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "start_hour" in str(exc_info.value)

    def test_block_missing_end_hour(self):
        block_yaml = textwrap.dedent("""\
            - name: Block
              start_hour: 6
              offline: true
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_block(block_yaml))
        assert "end_hour" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. Validation errors — show fields
# ---------------------------------------------------------------------------


class TestShowFieldValidation:
    """Tests for show field validation."""

    def _yaml_with_show(self, show_yaml: str) -> str:
        return textwrap.dedent("""\
            id: ch1
            name: Test Channel
            number: 42
            color: "#ff6600"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
        """) + textwrap.indent(show_yaml, "          ")

    def test_show_missing_name(self):
        show_yaml = textwrap.dedent("""\
            - weight: 1
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "name" in str(exc_info.value)

    def test_show_missing_weight(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "weight" in str(exc_info.value)

    def test_show_weight_zero(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 0
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "weight" in str(exc_info.value)

    def test_show_weight_1001(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 1001
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "weight" in str(exc_info.value)

    def test_show_weight_negative(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: -5
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "weight" in str(exc_info.value)

    def test_show_missing_jellyfin_ids(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 1
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "jellyfin_ids" in str(exc_info.value)

    def test_show_jellyfin_ids_empty_list(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 1
              jellyfin_ids: []
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "jellyfin_ids" in str(exc_info.value)

    def test_show_jellyfin_ids_not_list(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 1
              jellyfin_ids: abc123
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "jellyfin_ids" in str(exc_info.value)

    def test_show_jellyfin_ids_contains_non_string(self):
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: 1
              jellyfin_ids:
                - 12345
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "jellyfin_ids" in str(exc_info.value)

    def test_show_weight_boolean_rejected(self):
        """YAML booleans (true/false) must not be accepted as weight integers."""
        show_yaml = textwrap.dedent("""\
            - name: Show
              weight: true
              jellyfin_ids: [abc]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(self._yaml_with_show(show_yaml))
        assert "weight" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 6. Overlapping blocks
# ---------------------------------------------------------------------------


class TestOverlappingBlocks:
    """Tests for block overlap detection (Req 5.4)."""

    def test_overlapping_normal_blocks_rejected(self):
        """Two normal blocks that overlap (8-12 and 10-14) raise ConfigValidationError."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 8
                end_hour: 12
                shows:
                  - name: Show A
                    weight: 1
                    jellyfin_ids: [id-a]
              - name: Overlap
                start_hour: 10
                end_hour: 14
                shows:
                  - name: Show B
                    weight: 1
                    jellyfin_ids: [id-b]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "overlap" in str(exc_info.value).lower()

    def test_non_overlapping_blocks_accepted(self):
        """Two adjacent non-overlapping blocks parse fine."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show A
                    weight: 1
                    jellyfin_ids: [id-a]
              - name: Afternoon
                start_hour: 12
                end_hour: 18
                shows:
                  - name: Show B
                    weight: 1
                    jellyfin_ids: [id-b]
        """)
        channel = parse(yaml_str)
        assert len(channel.blocks) == 2

    def test_midnight_crossing_overlaps_normal_block(self):
        """A midnight-crossing block (22-4) overlapping with a normal block (2-8) is detected."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: LateNight
                start_hour: 22
                end_hour: 4
                offline: true
              - name: EarlyMorning
                start_hour: 2
                end_hour: 8
                shows:
                  - name: Show A
                    weight: 1
                    jellyfin_ids: [id-a]
        """)
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(yaml_str)
        assert "overlap" in str(exc_info.value).lower()

    def test_midnight_crossing_non_overlapping_accepted(self):
        """A midnight-crossing block (22-6) and a normal block (6-22) share exactly hour 6 and 22 as boundaries — no overlap."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Daytime
                start_hour: 6
                end_hour: 22
                shows:
                  - name: Show A
                    weight: 1
                    jellyfin_ids: [id-a]
              - name: Overnight
                start_hour: 22
                end_hour: 6
                offline: true
        """)
        channel = parse(yaml_str)
        assert len(channel.blocks) == 2

    def test_adjacent_blocks_not_overlapping(self):
        """Three back-to-back 8-hour blocks don't overlap."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 0
                end_hour: 8
                offline: true
              - name: Daytime
                start_hour: 8
                end_hour: 16
                shows:
                  - name: Show A
                    weight: 1
                    jellyfin_ids: [id-a]
              - name: Evening
                start_hour: 16
                end_hour: 24
                offline: true
        """)
        channel = parse(yaml_str)
        assert len(channel.blocks) == 3

    def test_identical_block_ranges_overlap(self):
        """Two blocks with identical start/end hours overlap."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Block A
                start_hour: 6
                end_hour: 12
                offline: true
              - name: Block B
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [id-a]
        """)
        with pytest.raises(ConfigValidationError):
            parse(yaml_str)


# ---------------------------------------------------------------------------
# 7. load_all_channels — duplicate channel numbers
# ---------------------------------------------------------------------------


class TestLoadAllChannels:
    """Tests for load_all_channels() using tmp_path filesystem fixtures."""

    def test_single_valid_file_loads(self, tmp_path):
        """A single valid YAML file is loaded correctly."""
        _write_yaml(tmp_path, "ch1.yaml", MINIMAL_VALID_YAML)
        channels = load_all_channels(str(tmp_path))
        assert "test-channel" in channels
        assert channels["test-channel"].number == 42

    def test_duplicate_channel_numbers_excluded(self, tmp_path):
        """Two YAML files sharing the same channel number are both excluded."""
        yaml_a = textwrap.dedent("""\
            id: channel-a
            name: Channel A
            number: 10
            color: "#aaaaaa"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        yaml_b = textwrap.dedent("""\
            id: channel-b
            name: Channel B
            number: 10
            color: "#bbbbbb"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        _write_yaml(tmp_path, "a.yaml", yaml_a)
        _write_yaml(tmp_path, "b.yaml", yaml_b)
        channels = load_all_channels(str(tmp_path))
        assert "channel-a" not in channels
        assert "channel-b" not in channels
        assert len(channels) == 0

    def test_duplicate_number_does_not_affect_unique_channels(self, tmp_path):
        """A duplicate-number pair is excluded, but other unique channels load fine."""
        yaml_dup_a = textwrap.dedent("""\
            id: dup-a
            name: Dup A
            number: 99
            color: "#aaaaaa"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        yaml_dup_b = textwrap.dedent("""\
            id: dup-b
            name: Dup B
            number: 99
            color: "#bbbbbb"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        yaml_unique = textwrap.dedent("""\
            id: unique-ch
            name: Unique Channel
            number: 42
            color: "#cccccc"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        _write_yaml(tmp_path, "dup_a.yaml", yaml_dup_a)
        _write_yaml(tmp_path, "dup_b.yaml", yaml_dup_b)
        _write_yaml(tmp_path, "unique.yaml", yaml_unique)
        channels = load_all_channels(str(tmp_path))
        assert "dup-a" not in channels
        assert "dup-b" not in channels
        assert "unique-ch" in channels

    def test_invalid_file_skipped_valid_loaded(self, tmp_path):
        """An invalid YAML file is skipped; valid files are still loaded."""
        invalid_yaml = textwrap.dedent("""\
            id: bad-channel
            color: not-valid-color
            blocks: []
        """)
        _write_yaml(tmp_path, "invalid.yaml", invalid_yaml)
        _write_yaml(tmp_path, "valid.yaml", MINIMAL_VALID_YAML)
        channels = load_all_channels(str(tmp_path))
        assert "test-channel" in channels
        assert "bad-channel" not in channels

    def test_empty_directory_returns_empty_dict(self, tmp_path):
        """An empty config directory returns an empty dict."""
        channels = load_all_channels(str(tmp_path))
        assert channels == {}

    def test_non_yaml_files_ignored(self, tmp_path):
        """Non-.yaml files in the directory are ignored."""
        (tmp_path / "readme.txt").write_text("not a channel", encoding="utf-8")
        (tmp_path / "channel.json").write_text('{"id": "ch1"}', encoding="utf-8")
        _write_yaml(tmp_path, "valid.yaml", MINIMAL_VALID_YAML)
        channels = load_all_channels(str(tmp_path))
        assert len(channels) == 1
        assert "test-channel" in channels

    def test_nonexistent_directory_returns_empty_dict(self):
        """A nonexistent config directory returns an empty dict without crashing."""
        channels = load_all_channels("/nonexistent/path/that/does/not/exist")
        assert channels == {}

    def test_duplicate_channel_id_in_separate_files_skips_second(self, tmp_path):
        """A duplicate channel id (second file) is skipped; first is kept."""
        yaml1 = textwrap.dedent("""\
            id: same-id
            name: First Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        yaml2 = textwrap.dedent("""\
            id: same-id
            name: Second Channel
            number: 2
            color: "#bbbbbb"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                offline: true
        """)
        _write_yaml(tmp_path, "a_first.yaml", yaml1)
        _write_yaml(tmp_path, "b_second.yaml", yaml2)
        channels = load_all_channels(str(tmp_path))
        # Only one channel with this id survives
        assert len([k for k in channels if k == "same-id"]) == 1


# ---------------------------------------------------------------------------
# 8. Malformed YAML
# ---------------------------------------------------------------------------


class TestMalformedYaml:
    """Tests for malformed YAML input."""

    def test_invalid_yaml_syntax_raises_config_validation_error(self):
        """Malformed YAML raises ConfigValidationError, not yaml.YAMLError."""
        bad_yaml = "id: [unclosed bracket\nname: foo"
        with pytest.raises(ConfigValidationError) as exc_info:
            parse(bad_yaml)
        assert "YAML" in str(exc_info.value) or "parse" in str(exc_info.value).lower()

    def test_yaml_is_list_not_mapping(self):
        """A YAML document that is a list, not a mapping, raises ConfigValidationError."""
        yaml_str = textwrap.dedent("""\
            - id: ch1
            - name: Channel
        """)
        with pytest.raises(ConfigValidationError):
            parse(yaml_str)

    def test_yaml_is_scalar(self):
        """A YAML document that is a scalar raises ConfigValidationError."""
        with pytest.raises(ConfigValidationError):
            parse("just a string")

    def test_empty_string_raises(self):
        """An empty string raises ConfigValidationError (parsed as None by yaml.safe_load)."""
        with pytest.raises(ConfigValidationError):
            parse("")

    def test_none_document_raises(self):
        """A YAML document that is only comments/whitespace (parses as None) raises ConfigValidationError."""
        with pytest.raises(ConfigValidationError):
            parse("# just a comment\n")


# ---------------------------------------------------------------------------
# 9. Error type guarantees — never raw Python exceptions
# ---------------------------------------------------------------------------


class TestErrorTypeGuarantees:
    """All validation failures must raise ConfigValidationError, never KeyError/TypeError/etc."""

    def _assert_only_config_validation_error(self, yaml_str: str):
        """Assert that parse() raises ConfigValidationError and nothing else."""
        with pytest.raises(ConfigValidationError):
            parse(yaml_str)
        # Implicitly verifies no KeyError, TypeError, AttributeError, etc. propagate

    def test_show_not_a_dict_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - not_a_dict_value
        """)
        self._assert_only_config_validation_error(yaml_str)

    def test_block_not_a_dict_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - not_a_block
        """)
        self._assert_only_config_validation_error(yaml_str)

    def test_shows_list_not_list_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows: "not a list"
        """)
        self._assert_only_config_validation_error(yaml_str)

    def test_number_as_string_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: "forty-two"
            color: "#aaaaaa"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                offline: true
        """)
        self._assert_only_config_validation_error(yaml_str)

    def test_start_hour_as_string_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - name: Morning
                start_hour: "six"
                end_hour: 12
                offline: true
        """)
        self._assert_only_config_validation_error(yaml_str)

    def test_weight_as_string_raises_config_error(self):
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel
            number: 1
            color: "#aaaaaa"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: "heavy"
                    jellyfin_ids: [abc]
        """)
        self._assert_only_config_validation_error(yaml_str)


# ---------------------------------------------------------------------------
# 10. Edge cases and additional coverage
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Additional edge cases for thorough coverage."""

    def test_single_block_full_day(self):
        """A single block covering 0-24 is valid."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: All Day
                start_hour: 0
                end_hour: 24
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [id1]
        """)
        channel = parse(yaml_str)
        assert channel.blocks[0].start_hour == 0
        assert channel.blocks[0].end_hour == 24

    def test_multiple_shows_in_block(self):
        """A block with multiple shows all parse correctly."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show A
                    weight: 3
                    jellyfin_ids: [id1, id2]
                  - name: Show B
                    weight: 1
                    jellyfin_ids: [id3]
                  - name: Show C
                    weight: 2
                    jellyfin_ids: [id4, id5, id6]
        """)
        channel = parse(yaml_str)
        assert len(channel.blocks[0].shows) == 3
        assert channel.blocks[0].shows[0].weight == 3
        assert channel.blocks[0].shows[2].jellyfin_ids == ["id4", "id5", "id6"]

    def test_multiple_jellyfin_ids_preserved(self):
        """Multiple jellyfin_ids are preserved exactly as specified."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids:
                      - aaaaa
                      - bbbbb
                      - ccccc
        """)
        channel = parse(yaml_str)
        assert channel.blocks[0].shows[0].jellyfin_ids == ["aaaaa", "bbbbb", "ccccc"]

    def test_block_offline_false_field_ignored(self):
        """A block with 'offline: false' and a shows list is treated as a shows block."""
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
            blocks:
              - name: Morning
                start_hour: 6
                end_hour: 12
                offline: false
                shows:
                  - name: Show
                    weight: 1
                    jellyfin_ids: [id1]
        """)
        channel = parse(yaml_str)
        assert channel.blocks[0].offline is False
        assert channel.blocks[0].shows is not None

    def test_max_48_blocks_accepted(self):
        """Exactly 48 non-overlapping single-hour blocks (hours 0-23 can only give 24, so use offline all day + creative approach)."""
        # We can fit at most 24 non-overlapping 1-hour blocks (hours 0-23, end 1-24).
        # Let's test with 24 blocks to verify the limit isn't too low.
        blocks_lines = []
        for i in range(24):
            blocks_lines.append(
                f"  - name: Block{i}\n    start_hour: {i}\n    end_hour: {i+1}\n    offline: true"
            )
        yaml_str = textwrap.dedent("""\
            id: ch1
            name: Channel 1
            number: 1
            color: "#aabbcc"
        """) + "blocks:\n" + "\n".join(blocks_lines) + "\n"
        channel = parse(yaml_str)
        assert len(channel.blocks) == 24

    def test_serialize_shows_list_empty_shows_none(self):
        """A Channel with an offline block serializes without a shows key."""
        channel = Channel(
            id="ch1",
            name="Channel 1",
            number=1,
            color="#aabbcc",
            blocks=[
                Block(name="Night", start_hour=0, end_hour=24, shows=None, offline=True)
            ],
        )
        raw = yaml.safe_load(serialize(channel))
        assert raw["blocks"][0].get("offline") is True
        assert "shows" not in raw["blocks"][0]

    def test_serialize_show_preserves_all_fields(self):
        """serialize() preserves all show fields: name, weight, jellyfin_ids."""
        channel = Channel(
            id="ch1",
            name="Channel 1",
            number=1,
            color="#aabbcc",
            blocks=[
                Block(
                    name="Morning",
                    start_hour=6,
                    end_hour=12,
                    shows=[Show(name="Hey Arnold", weight=5, jellyfin_ids=["id1", "id2"])],
                    offline=False,
                )
            ],
        )
        raw = yaml.safe_load(serialize(channel))
        show = raw["blocks"][0]["shows"][0]
        assert show["name"] == "Hey Arnold"
        assert show["weight"] == 5
        assert show["jellyfin_ids"] == ["id1", "id2"]
