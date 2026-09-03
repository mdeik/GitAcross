from __future__ import annotations

from pathlib import Path
from typing import final

import yaml


@final
class State:
    """Persistent state tracking which releases have been synced.

    State lives in ``state.yml`` inside the work directory (the directory
    passed to the constructor). Releases are keyed by tag name (not API
    release id) so state stays valid across source modes (`release` vs
    `tag`). Written atomically (tempfile + rename) after each successful
    release.
    """

    def __init__(self, work_dir):
        self.work_dir = Path(work_dir)
        self.path = self.work_dir / "state.yml"
        self._data = self._load()

    def __repr__(self):
        return f"State(work_dir={str(self.work_dir)!r})"

    def _load(self):
        data = {}
        if self.path.exists():
            with open(self.path) as f:
                data = yaml.safe_load(f)
        # Tolerate degenerate or hand-edited YAML (e.g. a bare ``projects:``
        # key with no value, non-map documents, or null entries) so a corrupt
        # state file can never crash a run — it is simply treated as empty.
        if not isinstance(data, dict):
            data = {}
        projects = data.get("projects")
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        for name, project in list(projects.items()):
            if not isinstance(project, dict):
                projects[name] = {}
            elif not isinstance(project.get("releases"), dict):
                project["releases"] = {}
        return data

    def has_release(self, project_name, tag_name):
        releases = (
            self._data.get("projects", {}).get(project_name, {}).get("releases", {})
        )
        return str(tag_name) in releases

    def add_release(self, project_name, tag_name, data):
        self._data.setdefault("projects", {}).setdefault(project_name, {}).setdefault(
            "releases", {}
        )
        self._data["projects"][project_name]["releases"][str(tag_name)] = data

    def save(self):
        self.work_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False)
        _ = tmp.replace(self.path)
