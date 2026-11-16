"""Tests for ChannelList — channels.py"""

from unittest.mock import MagicMock


from channels import ChannelList
from schedule import ChannelInfo


def _make_channel_list(channel_ids: list[str]) -> ChannelList:
    """Build a ChannelList from a list of IDs using a mock client."""
    client = MagicMock()
    client.get_channels.return_value = [
        ChannelInfo(id=cid, name=cid.title()) for cid in channel_ids
    ]
    return ChannelList(client)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_all_channels_matches_server_order():
    cl = _make_channel_list(["sitcoms", "anime", "news"])
    ids = [c.id for c in cl.all_channels()]
    assert ids == ["sitcoms", "anime", "news"]


def test_channel_numbers_are_1_based():
    cl = _make_channel_list(["sitcoms", "anime"])
    assert cl.all_channels()[0].number == 1
    assert cl.all_channels()[1].number == 2


def test_empty_channel_list():
    client = MagicMock()
    client.get_channels.return_value = []
    cl = ChannelList(client)
    assert cl.all_channels() == []


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def test_next_channel_wraps_around():
    cl = _make_channel_list(["a", "b", "c"])
    assert cl.next_channel("c").id == "a"


def test_prev_channel_wraps_around():
    cl = _make_channel_list(["a", "b", "c"])
    assert cl.prev_channel("a").id == "c"


def test_next_channel_from_middle():
    cl = _make_channel_list(["a", "b", "c"])
    assert cl.next_channel("b").id == "c"


def test_prev_channel_from_middle():
    cl = _make_channel_list(["a", "b", "c"])
    assert cl.prev_channel("b").id == "a"


def test_next_channel_unknown_id_returns_first():
    cl = _make_channel_list(["a", "b"])
    assert cl.next_channel("unknown").id == "a"


def test_prev_channel_unknown_id_returns_last():
    cl = _make_channel_list(["a", "b"])
    assert cl.prev_channel("unknown").id == "b"


def test_next_channel_empty_list_returns_none():
    cl = _make_channel_list([])
    assert cl.next_channel("a") is None


def test_prev_channel_empty_list_returns_none():
    cl = _make_channel_list([])
    assert cl.prev_channel("a") is None


# ---------------------------------------------------------------------------
# get_channel_by_number
# ---------------------------------------------------------------------------


def test_get_channel_by_number_found():
    cl = _make_channel_list(["sitcoms", "anime"])
    ch = cl.get_channel_by_number(2)
    assert ch is not None
    assert ch.id == "anime"


def test_get_channel_by_number_not_found():
    cl = _make_channel_list(["sitcoms"])
    assert cl.get_channel_by_number(99) is None


# ---------------------------------------------------------------------------
# get_channel_by_id
# ---------------------------------------------------------------------------


def test_get_channel_by_id_found():
    cl = _make_channel_list(["sitcoms", "anime"])
    ch = cl.get_channel_by_id("anime")
    assert ch is not None
    assert ch.number == 2


def test_get_channel_by_id_not_found():
    cl = _make_channel_list(["sitcoms"])
    assert cl.get_channel_by_id("nope") is None


# ---------------------------------------------------------------------------
# Server number field — task 10
# ---------------------------------------------------------------------------


def _make_channel_list_with_numbers(infos: list[ChannelInfo]) -> ChannelList:
    """Build a ChannelList from explicit ChannelInfo objects."""
    client = MagicMock()
    client.get_channels.return_value = infos
    return ChannelList(client)


def test_server_number_used_when_nonzero():
    """Server number > 0 overrides positional assignment."""
    infos = [ChannelInfo(id="sitcoms", name="Sitcoms", number=5)]
    cl = _make_channel_list_with_numbers(infos)
    assert cl.all_channels()[0].number == 5


def test_server_number_zero_falls_back_to_positional():
    """Server number == 0 falls back to 1-based positional index."""
    infos = [
        ChannelInfo(id="sitcoms", name="Sitcoms", number=0),
        ChannelInfo(id="anime", name="Anime", number=0),
    ]
    cl = _make_channel_list_with_numbers(infos)
    assert cl.all_channels()[0].number == 1
    assert cl.all_channels()[1].number == 2


def test_mixed_server_numbers_and_positional_fallback():
    """Channels with server numbers use them; those with 0 use positional."""
    infos = [
        ChannelInfo(id="news", name="News", number=10),
        ChannelInfo(id="sitcoms", name="Sitcoms", number=0),
        ChannelInfo(id="anime", name="Anime", number=7),
    ]
    cl = _make_channel_list_with_numbers(infos)
    channels = cl.all_channels()
    assert channels[0].number == 10  # server-assigned
    assert channels[1].number == 2  # positional fallback (index 1 → 2)
    assert channels[2].number == 7  # server-assigned


def test_existing_channel_list_helper_still_uses_positional():
    """Regression: the no-number helper still produces 1-based numbers."""
    cl = _make_channel_list(["a", "b", "c"])
    numbers = [c.number for c in cl.all_channels()]
    assert numbers == [1, 2, 3]


def test_partly_numbered_lineup_never_collides():
    """Positional fallback numbers skip any number already taken."""
    infos = [
        ChannelInfo(id="a", name="A", number=2),
        ChannelInfo(id="b", name="B", number=0),
        ChannelInfo(id="c", name="C", number=0),
        ChannelInfo(id="d", name="D", number=0),
    ]
    cl = _make_channel_list_with_numbers(infos)
    numbers = [c.number for c in cl.all_channels()]
    assert numbers == [2, 3, 4, 5]
    assert len(set(numbers)) == len(numbers)
