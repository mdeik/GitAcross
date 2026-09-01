"""Tests for persistent state — split from the original single-file suite."""
from __future__ import annotations

import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def test_config_empty_projects():
    from gitacross.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        # Bare `projects:` key (no list) must not crash
        bare = Path(tmp) / "bare.yml"
        _ = bare.write_text("projects:\n")
        assert Config(str(bare)).projects == []

        empty = Path(tmp) / "empty.yml"
        _ = empty.write_text("projects: []\n")
        assert Config(str(empty)).projects == []

    print("  ✓ config: empty projects list parses to zero projects")


def test_config_empty_project_entry():
    """An empty (or non-map) project entry fails with a clear error, not a KeyError."""
    from gitacross.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        empty = Path(tmp) / "empty_entry.yml"
        _ = empty.write_text("projects:\n  - {}\n")
        try:
            _ = Config(str(empty))
            assert False, "should have raised"
        except ValueError as e:
            assert "'name'" in str(e)

        notmap = Path(tmp) / "notmap.yml"
        _ = notmap.write_text("projects:\n  - 42\n")
        try:
            _ = Config(str(notmap))
            assert False, "should have raised"
        except TypeError as e:
            assert "map/dict" in str(e)

    print("  ✓ config: empty/non-map project entries raise a clear error")


def test_state():
    from gitacross.state import State

    with tempfile.TemporaryDirectory() as tmp:
        state = State(tmp)

        assert not state.has_release("myapp", "v1.0")
        state.add_release(
            "myapp",
            "v1.0",
            {
                "tag": "v1.0",
                "target_commit": "abc123",
                "published_at": "2026-07-07T10:00:00Z",
            },
        )
        state.save()

        state2 = State(tmp)
        assert state2.has_release("myapp", "v1.0")
        assert not state2.has_release("myapp", "v2.0")

        print("  ✓ state persistence")



