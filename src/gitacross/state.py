from pathlib import Path

import yaml


class State:
    """Persistent state tracking which releases have been synced.

    Releases are keyed by tag name (not API release id) so state stays
    valid across source modes (`release` vs `tag`). Written atomically
    (tempfile + rename) after each successful release.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._data = self._load()

    def _load(self):
        if self.path.exists():
            with open(self.path) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        self._migrate_entries(data)
        return data

    @staticmethod
    def _migrate_entries(data):
        """Re-key release entries by tag name and migrate field names."""
        projects = data.get("projects") or {}
        for project in projects.values():
            releases = project.get("releases")
            if not isinstance(releases, dict):
                continue
            for key, entry in list(releases.items()):
                if not isinstance(entry, dict):
                    continue
                # Remove deprecated commit_sha
                entry.pop("commit_sha", None)
                # Rename published_at -> source_date
                if "published_at" in entry and "source_date" not in entry:
                    entry["source_date"] = entry.pop("published_at")
                tag = entry.get("tag")
                if not tag or str(key) == str(tag):
                    continue
                if str(tag) not in releases:
                    releases[str(tag)] = entry
                del releases[key]

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False)
        tmp.replace(self.path)
