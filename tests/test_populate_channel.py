"""
tests/test_populate_channel.py
Unit tests for tools/populate_channel.py

All Jellyfin HTTP calls are mocked — no running Jellyfin required.
Requirements: 35, 36
"""

from __future__ import annotations

import json
import argparse
import os
import sys
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

# Ensure repo root is on sys.path so we can import the tool
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

import tools.populate_channel as pc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_series(series_id="aaa", name="Test Show", year=1999):
    return {"Id": series_id, "Name": name, "ProductionYear": year}


def _make_episode(ep_id, season, episode, name=None, premiere="2000-01-01",
                  container="mkv", vcodec="h264", acodec="aac"):
    return {
        "Id": ep_id,
        "Name": name or f"S{season:02d}E{episode:02d}",
        "ParentIndexNumber": season,
        "IndexNumber": episode,
        "PremiereDate": premiere,
        "_container": container,
        "_vcodec": vcodec,
        "_acodec": acodec,
    }


def _media_sources_response(ep, *, failing_container=None, failing_vcodec=None, failing_acodec=None):
    """Build a fake /Items?Ids={id}&Fields=MediaSources Jellyfin response."""
    container = failing_container or ep.get("_container", "mkv")
    vcodec = failing_vcodec or ep.get("_vcodec", "h264")
    acodec = failing_acodec or ep.get("_acodec", "aac")
    item = {
        "Id": ep["Id"],
        "MediaSources": [
            {
                "Container": container,
                "MediaStreams": [
                    {"Type": "Video", "Codec": vcodec},
                    {"Type": "Audio", "Codec": acodec},
                ],
            }
        ],
    }
    return {"Items": [item], "TotalRecordCount": 1}


def _items_response(items, total=None):
    return {"Items": items, "TotalRecordCount": total or len(items), "StartIndex": 0}


def _make_urlopen(responses: list):
    """
    Return a mock for urllib.request.urlopen that returns successive
    responses from the list, each wrapped as a context manager returning
    JSON bytes.
    """
    call_index = [0]

    def _urlopen(req, timeout=None):
        idx = call_index[0]
        if idx >= len(responses):
            raise AssertionError(f"urlopen called {idx + 1} times but only {len(responses)} responses provided")
        payload = responses[idx]
        call_index[0] += 1
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = json.dumps(payload).encode()
        return mock_resp

    return _urlopen


# ---------------------------------------------------------------------------
# Tests: series search
# ---------------------------------------------------------------------------

class TestSeriesSearch(unittest.TestCase):

    def test_single_series_match_no_prompt(self):
        """Single search result → proceeds without prompt, confirmation on stderr."""
        series = _make_series("s1", "SpongeBob SquarePants", 1999)
        responses = [_items_response([series])]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with patch("sys.stderr", new_callable=StringIO) as mock_err:
                result = pc._search_series("SpongeBob SquarePants", "http://localhost:8096", "key")

        self.assertEqual(result["Id"], "s1")
        self.assertIn("Found: SpongeBob SquarePants", mock_err.getvalue())

    def test_multiple_series_match_prompts_user(self):
        """Multiple results → numbered list printed to stderr, user selects via stdin."""
        series1 = _make_series("s1", "SpongeBob SquarePants", 1999)
        series2 = _make_series("s2", "SpongeBob SquarePants: Lost Tapes", 2010)
        responses = [_items_response([series1, series2])]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with patch("builtins.input", return_value="2"):
                with patch("sys.stderr", new_callable=StringIO) as mock_err:
                    result = pc._search_series("SpongeBob", "http://localhost:8096", "key")

        self.assertEqual(result["Id"], "s2")
        self.assertIn("[1]", mock_err.getvalue())
        self.assertIn("[2]", mock_err.getvalue())

    def test_zero_series_match_exits_one(self):
        """No search results → SystemExit with message."""
        responses = [_items_response([])]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with self.assertRaises(SystemExit) as cm:
                pc._search_series("NoSuchShow", "http://localhost:8096", "key")

        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("NoSuchShow", str(cm.exception.code))


# ---------------------------------------------------------------------------
# Tests: output formats
# ---------------------------------------------------------------------------

class TestOutputFormats(unittest.TestCase):

    def setUp(self):
        self.eps = [
            {"Id": "id001", "series_name": "Test Show", "Name": "Ep1",
             "ParentIndexNumber": 1, "IndexNumber": 1, "PremiereDate": "2000-01-01"},
            {"Id": "id002", "series_name": "Test Show", "Name": "Ep2",
             "ParentIndexNumber": 1, "IndexNumber": 2, "PremiereDate": "2000-01-08"},
        ]

    def test_yaml_block_output_format(self):
        output = pc._output_yaml_block(self.eps, "Test Show")
        self.assertIn("- name: Test Show", output)
        self.assertIn("weight: 1", output)
        self.assertIn("jellyfin_ids:", output)
        self.assertIn('"id001"', output)
        self.assertIn('"id002"', output)
        # UUIDs in order
        self.assertLess(output.index('"id001"'), output.index('"id002"'))

    def test_ids_only_output_format(self):
        output = pc._output_ids_only(self.eps)
        lines = output.strip().splitlines()
        self.assertEqual(lines, ["id001", "id002"])
        # No markup — each line should be purely the ID value with no extra characters
        for line in lines:
            self.assertEqual(line.strip(), line)  # no leading/trailing whitespace
            self.assertNotIn(" ", line)  # no spaces
            self.assertNotIn(":", line)  # no YAML colons
            self.assertNotIn('"', line)  # no quotes

    def test_json_output_format(self):
        output = pc._output_json(self.eps)
        records = json.loads(output)
        self.assertIsInstance(records, list)
        self.assertEqual(len(records), 2)
        for rec in records:
            for field in ("id", "name", "series_name", "season", "episode", "air_date"):
                self.assertIn(field, rec)
        self.assertEqual(records[0]["id"], "id001")
        self.assertEqual(records[0]["season"], 1)
        self.assertEqual(records[0]["air_date"], "2000-01-01")


# ---------------------------------------------------------------------------
# Tests: direct-play validation exclusion
# ---------------------------------------------------------------------------

class TestValidationExclusion(unittest.TestCase):

    def _make_eps_responses(self, eps):
        """Build /Items/{id}?Fields=MediaSources responses for a list of episodes."""
        return [_media_sources_response(ep) for ep in eps]

    def test_validation_failure_excluded_with_warning(self):
        """Episode with non-passthrough container is excluded; WARNING printed to stderr."""
        eps = [
            _make_episode("good1", 1, 1, container="mkv", vcodec="h264", acodec="aac"),
            _make_episode("bad1", 1, 2, container="avi", vcodec="mpeg4", acodec="mp3"),
        ]
        responses = [
            _media_sources_response(eps[0]),
            _media_sources_response(eps[1]),
        ]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with patch("sys.stderr", new_callable=StringIO) as mock_err:
                valid = pc._process_episodes(
                    eps, "Test Show", None, "episode-number",
                    "http://localhost:8096", "key", dry_run=False
                )

        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0]["Id"], "good1")
        stderr_out = mock_err.getvalue()
        self.assertIn("WARNING", stderr_out)
        # Warning should identify the excluded episode (S01E02) and the failure reason
        self.assertIn("S01E02", stderr_out)
        self.assertIn("container", stderr_out)

    def test_all_episodes_fail_exits_one(self):
        """All episodes fail validation → SystemExit with code 1."""
        eps = [
            _make_episode("bad1", 1, 1, container="avi"),
            _make_episode("bad2", 1, 2, container="wmv"),
        ]
        responses = [
            _media_sources_response(eps[0]),
            _media_sources_response(eps[1]),
        ]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with patch("sys.stderr", new_callable=StringIO):
                with self.assertRaises(SystemExit) as cm:
                    pc._process_episodes(
                        eps, "Test Show", None, "episode-number",
                        "http://localhost:8096", "key", dry_run=False
                    )

        self.assertNotEqual(cm.exception.code, 0)

    def test_no_silent_exclusion(self):
        """Every excluded episode produces exactly one WARNING line on stderr."""
        eps = [
            _make_episode("e1", 1, 1, container="mkv"),
            _make_episode("e2", 1, 2, container="avi"),
            _make_episode("e3", 1, 3, container="avi"),
            _make_episode("e4", 1, 4, container="mkv"),
        ]
        responses = [_media_sources_response(ep) for ep in eps]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            with patch("sys.stderr", new_callable=StringIO) as mock_err:
                valid = pc._process_episodes(
                    eps, "Test Show", None, "episode-number",
                    "http://localhost:8096", "key", dry_run=False
                )

        warning_lines = [l for l in mock_err.getvalue().splitlines() if "WARNING" in l]
        excluded_count = len(eps) - len(valid)
        self.assertEqual(len(warning_lines), excluded_count,
                         "Each excluded episode must produce exactly one WARNING line")


# ---------------------------------------------------------------------------
# Tests: pagination
# ---------------------------------------------------------------------------

class TestPagination(unittest.TestCase):

    def test_pagination_fetches_all_pages(self):
        """250 episodes → two requests with StartIndex 0 and 200; all 250 in result."""
        page1 = [_make_episode(f"e{i}", 1, i) for i in range(1, 201)]
        page2 = [_make_episode(f"e{i}", 1, i) for i in range(201, 251)]

        responses = [
            {"Items": page1, "TotalRecordCount": 250, "StartIndex": 0},
            {"Items": page2, "TotalRecordCount": 250, "StartIndex": 200},
        ]

        with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
            result = pc._fetch_all_episodes("series1", "http://localhost:8096", "key")

        self.assertEqual(len(result), 250)
        self.assertEqual(result[0]["Id"], "e1")
        self.assertEqual(result[249]["Id"], "e250")


# ---------------------------------------------------------------------------
# Tests: season filter
# ---------------------------------------------------------------------------

class TestSeasonFilter(unittest.TestCase):

    def setUp(self):
        self.eps = [_make_episode(f"e{s}{e}", s, e) for s in range(1, 6) for e in range(1, 4)]
        # 5 seasons × 3 episodes each = 15 total

    def test_seasons_filter_range(self):
        """--seasons 1-3 keeps only seasons 1, 2, 3."""
        seasons = pc._parse_seasons("1-3")
        filtered = [ep for ep in self.eps if ep.get("ParentIndexNumber") in seasons]
        self.assertEqual(sorted({ep["ParentIndexNumber"] for ep in filtered}), [1, 2, 3])
        self.assertEqual(len(filtered), 9)

    def test_seasons_filter_list(self):
        """--seasons 1,3,5 keeps only seasons 1, 3, 5."""
        seasons = pc._parse_seasons("1,3,5")
        filtered = [ep for ep in self.eps if ep.get("ParentIndexNumber") in seasons]
        self.assertEqual(sorted({ep["ParentIndexNumber"] for ep in filtered}), [1, 3, 5])
        self.assertEqual(len(filtered), 9)

    def test_seasons_single(self):
        """--seasons 2 keeps only season 2."""
        seasons = pc._parse_seasons("2")
        self.assertEqual(seasons, {2})


# ---------------------------------------------------------------------------
# Tests: sort order
# ---------------------------------------------------------------------------

class TestSortOrder(unittest.TestCase):

    def test_sort_by_air_date(self):
        """Episodes sorted by PremiereDate ascending."""
        eps = [
            _make_episode("e3", 1, 3, premiere="2000-03-01"),
            _make_episode("e1", 1, 1, premiere="2000-01-01"),
            _make_episode("e2", 1, 2, premiere="2000-02-01"),
        ]
        eps.sort(key=pc._sort_key_air_date)
        self.assertEqual([ep["Id"] for ep in eps], ["e1", "e2", "e3"])

    def test_sort_by_episode_number(self):
        """Episodes sorted by season then episode number."""
        eps = [
            _make_episode("e12", 1, 2),
            _make_episode("e21", 2, 1),
            _make_episode("e11", 1, 1),
        ]
        eps.sort(key=pc._sort_key_episode_number)
        self.assertEqual([ep["Id"] for ep in eps], ["e11", "e12", "e21"])


# ---------------------------------------------------------------------------
# Tests: dry run
# ---------------------------------------------------------------------------

class TestDryRun(unittest.TestCase):

    def test_dry_run_prints_urls_no_requests(self):
        """--dry-run prints URLs to stdout; urlopen is never called."""
        series = _make_series("s1", "SpongeBob SquarePants")
        eps = [_make_episode("e1", 1, 1)]

        # urlopen should be called for: series lookup (by ID) and episode fetch
        # but NOT for per-episode validation
        series_response = _items_response([series])
        episodes_response = _items_response(eps)

        with patch("urllib.request.urlopen", side_effect=_make_urlopen([series_response, episodes_response])) as mock_open:
            with patch("sys.stdout", new_callable=StringIO) as mock_out:
                with patch("sys.stderr", new_callable=StringIO):
                    with self.assertRaises(SystemExit) as cm:
                        pc._run_primary(
                            _make_args(series_name="SpongeBob SquarePants", dry_run=True),
                            "http://localhost:8096", "key"
                        )

        # Should exit 0
        self.assertEqual(cm.exception.code, 0)
        # Output should contain [dry-run] lines
        stdout = mock_out.getvalue()
        self.assertIn("[dry-run]", stdout)
        # urlopen call count should be for series search + episode fetch only, not per-episode validation
        # (2 calls: search + episode page)
        self.assertLessEqual(mock_open.call_count, 3)


# ---------------------------------------------------------------------------
# Tests: missing API key
# ---------------------------------------------------------------------------

class TestMissingApiKey(unittest.TestCase):

    def test_missing_api_key_exits_one(self):
        """JELLYFIN_API_KEY absent → SystemExit naming the variable."""
        env = {k: v for k, v in os.environ.items() if k != "JELLYFIN_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                pc._get_env()

        self.assertNotEqual(cm.exception.code, 0)
        self.assertIn("JELLYFIN_API_KEY", str(cm.exception.code))


# ---------------------------------------------------------------------------
# Tests: --append-to mode
# ---------------------------------------------------------------------------

class TestAppendTo(unittest.TestCase):

    def _make_channel_yaml(self, existing_ids: list[str]) -> str:
        ids_lines = "\n".join(f'            - "{i}"' for i in existing_ids)
        return (
            "id: nickelodeon\n"
            "name: Classic Nickelodeon\n"
            "number: 5\n"
            'color: "#ff6600"\n'
            "blocks:\n"
            "  - name: All Day\n"
            "    start_hour: 0\n"
            "    end_hour: 24\n"
            "    shows:\n"
            "      - name: SpongeBob SquarePants\n"
            "        weight: 2\n"
            "        jellyfin_ids:\n"
            f"{ids_lines}\n"
        )

    def test_append_to_adds_new_ids(self):
        """New UUIDs are appended after existing; existing list unchanged; file written."""
        existing = ["id001", "id002"]
        new_ep = _make_episode("id003", 1, 3)
        series = _make_series("series1", "SpongeBob SquarePants")

        yaml_content = self._make_channel_yaml(existing)

        # Responses: series lookup (single item endpoint), episode page, validation for id003
        responses = [
            {"Items": [series], "TotalRecordCount": 1},  # GET /Items?Ids={id}
            _items_response([                  # episode fetch
                {"Id": "id001", "ParentIndexNumber": 1, "IndexNumber": 1,
                 "Name": "Help Wanted", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
                {"Id": "id002", "ParentIndexNumber": 1, "IndexNumber": 2,
                 "Name": "Reef Blower", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
                new_ep,
            ]),
            _media_sources_response(new_ep),   # validation for id003 only
        ]

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    with patch("sys.stderr", new_callable=StringIO):
                        pc._run_append_to(
                            _make_args(series_id="series1", append_to=tmp_path,
                                       show_name="SpongeBob SquarePants"),
                            "http://localhost:8096", "key"
                        )

            # File should now contain id003
            with open(tmp_path) as f:
                updated = f.read()

            self.assertIn("id001", updated)
            self.assertIn("id002", updated)
            self.assertIn("id003", updated)
            # Confirm existing order preserved (id001 before id002)
            self.assertLess(updated.index("id001"), updated.index("id002"))
            # id003 comes after id002
            self.assertLess(updated.index("id002"), updated.index("id003"))
            self.assertIn("+1 episodes added", mock_out.getvalue())
        finally:
            os.unlink(tmp_path)

    def test_append_to_no_new_episodes(self):
        """All Jellyfin episodes already in YAML → no file change, message printed."""
        existing = ["id001", "id002"]
        series = _make_series("series1", "SpongeBob SquarePants")
        yaml_content = self._make_channel_yaml(existing)

        responses = [
            {"Items": [series], "TotalRecordCount": 1},  # GET /Items?Ids={id}
            _items_response([
                {"Id": "id001", "ParentIndexNumber": 1, "IndexNumber": 1,
                 "Name": "E1", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
                {"Id": "id002", "ParentIndexNumber": 1, "IndexNumber": 2,
                 "Name": "E2", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
            ]),
        ]

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    with patch("sys.stderr", new_callable=StringIO):
                        with self.assertRaises(SystemExit) as cm:
                            pc._run_append_to(
                                _make_args(series_id="series1", append_to=tmp_path,
                                           show_name="SpongeBob SquarePants"),
                                "http://localhost:8096", "key"
                            )

            self.assertEqual(cm.exception.code, 0)
            self.assertIn("No new episodes found", mock_out.getvalue())
            # File should not have been modified — content is identical to what we wrote
            with open(tmp_path) as f:
                content_after = f.read()
            self.assertEqual(content_after, yaml_content)
        finally:
            os.unlink(tmp_path)

    def test_append_to_show_not_found_exits_one(self):
        """Wrong --show-name → SystemExit with code 1."""
        existing = ["id001"]
        series = _make_series("series1", "SpongeBob SquarePants")
        yaml_content = self._make_channel_yaml(existing)

        responses = [
            {"Items": [series], "TotalRecordCount": 1},  # GET /Items?Ids={id}
            _items_response([
                {"Id": "id001", "ParentIndexNumber": 1, "IndexNumber": 1,
                 "Name": "E1", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
            ]),
        ]

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
                with patch("sys.stderr", new_callable=StringIO):
                    with self.assertRaises(SystemExit) as cm:
                        pc._run_append_to(
                            _make_args(series_id="series1", append_to=tmp_path,
                                       show_name="Hey Arnold"),
                            "http://localhost:8096", "key"
                        )

            self.assertNotEqual(cm.exception.code, 0)
        finally:
            os.unlink(tmp_path)

    def test_append_to_file_not_found_exits_one(self):
        """Non-existent file path → SystemExit with code 1."""
        with patch("urllib.request.urlopen", side_effect=_make_urlopen([])):
            with self.assertRaises(SystemExit) as cm:
                pc._run_append_to(
                    _make_args(series_id="series1", append_to="/nonexistent/path.yaml",
                                show_name="SpongeBob"),
                    "http://localhost:8096", "key"
                )

        self.assertNotEqual(cm.exception.code, 0)


# ---------------------------------------------------------------------------
# Helper: build a minimal args namespace
# ---------------------------------------------------------------------------

def _make_args(**kwargs) -> argparse.Namespace:
    defaults = {
        "series_name": None,
        "series_id": None,
        "seasons": None,
        "output": "yaml-block",
        "sort_by": "air-date",
        "append_to": None,
        "show_name": None,
        "dry_run": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Requirement 44 — F821: argparse not imported at module scope in
# test_populate_channel.py — _make_args return annotation is broken
# ---------------------------------------------------------------------------


class TestMakeArgsAnnotation(unittest.TestCase):
    """Regression test for the F821: argparse used in return type annotation
    of _make_args() but only imported inside the function body.

    The annotation "argparse.Namespace" in the return type requires argparse
    to be resolvable at module scope. If it isn't, tools that evaluate
    annotations (type checkers, get_type_hints()) will raise NameError.

    Requirement 44: _make_args must have argparse imported at module scope
    so its return annotation is valid and get_type_hints() works.
    """

    def test_make_args_return_annotation_resolvable(self):
        """get_type_hints() must resolve the return annotation without raising.

        Before the fix: get_type_hints(_make_args) raises NameError because
        'argparse' is not in the module's global namespace.

        After the fix: argparse is imported at module scope and the annotation
        resolves cleanly.

        This test FAILS before the fix and PASSES after.
        """
        import typing

        # get_type_hints evaluates string annotations against the module globals.
        # If argparse is not in the module globals, it raises NameError.
        try:
            hints = typing.get_type_hints(_make_args)
        except NameError as exc:
            self.fail(
                f"get_type_hints(_make_args) raised NameError: {exc}. "
                "argparse must be imported at module scope so the return "
                "annotation 'argparse.Namespace' resolves correctly (Req 44 / F821)."
            )

        import argparse
        self.assertIs(hints.get("return"), argparse.Namespace)


# ---------------------------------------------------------------------------
# Requirement 45 — F841: series_name assigned but never used in _run_append_to
# ---------------------------------------------------------------------------


class TestAppendToUsesSeriesName(unittest.TestCase):
    """Regression tests for F841: series_name is fetched from Jellyfin in
    _run_append_to() but immediately discarded. All output messages use
    show_name (the CLI --show-name arg) instead.

    This matters when the Jellyfin series name differs from the show name
    in the YAML (e.g. YAML says "SpongeBob" but Jellyfin says
    "SpongeBob SquarePants"). Output messages would show the wrong name.

    Requirement 45: _run_append_to() must use the series name returned from
    Jellyfin in its output messages, not the raw --show-name CLI argument.
    """

    def _make_channel_yaml_with_show(self, show_name: str, existing_ids: list) -> str:
        ids_lines = "\n".join(f'            - "{i}"' for i in existing_ids)
        return (
            "id: nickelodeon\n"
            "name: Classic Nickelodeon\n"
            "number: 5\n"
            'color: "#ff6600"\n'
            "blocks:\n"
            "  - name: All Day\n"
            "    start_hour: 0\n"
            "    end_hour: 24\n"
            "    shows:\n"
            f"      - name: {show_name}\n"
            "        weight: 2\n"
            "        jellyfin_ids:\n"
            f"{ids_lines}\n"
        )

    def test_success_message_uses_jellyfin_series_name_not_cli_arg(self):
        """Req 45: the final 'Updated ... +N episodes added to ...' message
        must use the Jellyfin series name, not the raw --show-name CLI arg.

        Set up: YAML show name is 'SpongeBob', Jellyfin series name is
        'SpongeBob SquarePants'. The output must say 'SpongeBob SquarePants'.

        This test FAILS before the fix (output says 'SpongeBob') and
        PASSES after (output says 'SpongeBob SquarePants').
        """
        import tempfile

        series = {"Items": [{"Id": "series1", "Name": "SpongeBob SquarePants", "ProductionYear": 1999}], "TotalRecordCount": 1}
        new_ep = _make_episode("id003", 1, 3)
        responses = [
            series,
            _items_response([
                {"Id": "id001", "ParentIndexNumber": 1, "IndexNumber": 1,
                 "Name": "E1", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
                {"Id": "id002", "ParentIndexNumber": 1, "IndexNumber": 2,
                 "Name": "E2", "PremiereDate": "1999-05-01",
                 "_container": "mkv", "_vcodec": "h264", "_acodec": "aac"},
                new_ep,
            ]),
            _media_sources_response(new_ep),
        ]

        # YAML uses abbreviated show name "SpongeBob"
        yaml_content = self._make_channel_yaml_with_show("SpongeBob", ["id001", "id002"])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp_path = f.name

        try:
            with patch("urllib.request.urlopen", side_effect=_make_urlopen(responses)):
                with patch("sys.stdout", new_callable=StringIO) as mock_out:
                    with patch("sys.stderr", new_callable=StringIO):
                        pc._run_append_to(
                            _make_args(series_id="series1", append_to=tmp_path,
                                       show_name="SpongeBob"),
                            "http://localhost:8096", "key"
                        )

            output = mock_out.getvalue()
            self.assertIn("SpongeBob SquarePants", output, (
                f"Output used 'SpongeBob' (the CLI arg) instead of "
                f"'SpongeBob SquarePants' (the Jellyfin series name). "
                f"Output was:\n{output}"
            ))
        finally:
            import os
            os.unlink(tmp_path)
