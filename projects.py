"""Projects: separate programs, each with its own saved tools.

A project is a folder under ``artifacts/agent/projects/<id>/`` holding
``project.json`` (name, description, timestamps) and ``tools.json`` (that
project's tool registry). The built-in project ``scratch`` is the original
shared registry (``artifacts/agent/tools.json``); it always exists and cannot
be renamed or deleted.

Deleting a project moves its folder to ``projects/.trash/`` so nothing is
erased by a click.
"""
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

BUILTIN = "scratch"
MAX_NAME, MAX_DESCRIPTION = 60, 200
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def slug(name):
    """A short, filesystem-safe id stem for NAME."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:24] or "project"


def clean_fields(name=None, description=None):
    """``(fields, error)``: validated, trimmed name/description (only those given)."""
    out = {}
    if name is not None:
        name = " ".join(str(name).split())
        if not name:
            return None, "a project needs a name"
        if len(name) > MAX_NAME:
            return None, "project name is too long (max %d characters)" % MAX_NAME
        out["name"] = name
    if description is not None:
        description = " ".join(str(description).split())
        if len(description) > MAX_DESCRIPTION:
            return None, "description is too long (max %d characters)" % MAX_DESCRIPTION
        out["description"] = description
    return out, None


class ProjectStore:
    """CRUD over project folders. Thread-safe; every method is one small step."""

    def __init__(self, agent_dir):
        self.agent_dir = Path(agent_dir)
        self.root = self.agent_dir / "projects"
        self._lock = threading.Lock()

    # -- paths -----------------------------------------------------------
    def exists(self, project_id):
        return project_id == BUILTIN or (
            isinstance(project_id, str) and bool(_ID_RE.match(project_id))
            and (self.root / project_id / "project.json").is_file())

    def resolve(self, project_id):
        """A known project id; unknown or missing ids fall back to the built-in."""
        return project_id if self.exists(project_id) else BUILTIN

    def tools_path(self, project_id):
        pid = self.resolve(project_id)
        return self.agent_dir / "tools.json" if pid == BUILTIN \
            else self.root / pid / "tools.json"

    # -- read ------------------------------------------------------------
    def _tool_count(self, project_id):
        try:
            data = json.loads(self.tools_path(project_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, list):
            return 0
        # retired leftovers stay on disk but are no longer part of the project
        return sum(1 for t in data if isinstance(t, dict) and not t.get("retired"))

    def _own_counts(self, project_id):
        """How many functions of its own the project has per model: ``{"demo": n, "live": n}``.

        Kit helpers supplied by the harness and retired leftovers are not counted,
        so the number matches what the project actually built.
        """
        counts = {"demo": 0, "live": 0}
        try:
            data = json.loads(self.tools_path(project_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return counts
        for t in data if isinstance(data, list) else []:
            if isinstance(t, dict) and not t.get("retired") and not t.get("kit"):
                mode = t.get("mode") or "demo"
                if mode in counts:
                    counts[mode] += 1
        return counts

    def _builtin(self):
        return {"id": BUILTIN, "name": "Scratchpad",
                "description": "Shared tools, the guided demo and one-off prompts.",
                "created": 0, "updated": 0, "builtin": True,
                "tools": self._tool_count(BUILTIN), "counts": self._own_counts(BUILTIN)}

    def get(self, project_id):
        if project_id == BUILTIN:
            return self._builtin()
        if not self.exists(project_id):
            return None
        try:
            meta = json.loads((self.root / project_id / "project.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return {"id": project_id, "name": meta.get("name") or project_id,
                "description": meta.get("description") or "",
                "created": meta.get("created") or 0, "updated": meta.get("updated") or 0,
                "builtin": False, "tools": self._tool_count(project_id),
                "counts": self._own_counts(project_id)}

    def list(self):
        """Built-in first, then the others by creation time."""
        others = []
        if self.root.is_dir():
            for folder in self.root.iterdir():
                if folder.is_dir() and not folder.name.startswith("."):
                    meta = self.get(folder.name)
                    if meta:
                        others.append(meta)
        return [self._builtin()] + sorted(others, key=lambda p: p["created"])

    # -- write -----------------------------------------------------------
    def _save(self, project_id, meta):
        folder = self.root / project_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "project.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def create(self, name, description=""):
        """``(project, error)``."""
        fields, err = clean_fields(name, description or "")
        if err:
            return None, err
        with self._lock:
            if any(p["name"].lower() == fields["name"].lower() for p in self.list()):
                return None, "a project named %s already exists" % fields["name"]
            pid = "%s-%s" % (slug(fields["name"]), uuid.uuid4().hex[:4])
            now = round(time.time(), 3)
            self._save(pid, dict(fields, created=now, updated=now))
        return self.get(pid), None

    def update(self, project_id, name=None, description=None):
        """``(project, error)``; only the given fields change."""
        if project_id == BUILTIN:
            return None, "the built-in project cannot be renamed"
        if not self.exists(project_id):
            return None, "unknown project"
        fields, err = clean_fields(name, description)
        if err:
            return None, err
        with self._lock:
            if "name" in fields and any(
                    p["id"] != project_id and p["name"].lower() == fields["name"].lower()
                    for p in self.list()):
                return None, "a project named %s already exists" % fields["name"]
            cur = self.get(project_id)
            meta = {"name": cur["name"], "description": cur["description"],
                    "created": cur["created"], "updated": round(time.time(), 3)}
            meta.update(fields)
            self._save(project_id, meta)
        return self.get(project_id), None

    MAX_REQUIREMENTS_CHARS = 8000

    def requirements_path(self, project_id):
        """Where a project keeps the requirements its owner wrote (plain text, one per line)."""
        if project_id == BUILTIN:
            return self.root.parent / "requirements.txt"
        return self.root / project_id / "requirements.txt"

    def requirements(self, project_id):
        """The requirements text of a project, exactly as written; empty when there is none."""
        try:
            return self.requirements_path(project_id).read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return ""

    def set_requirements(self, project_id, text):
        """``(ok, error)``. Saves TEXT as written; only the owner's own edit changes it."""
        if not isinstance(text, str):
            return False, "requirements must be text"
        if len(text) > self.MAX_REQUIREMENTS_CHARS:
            return False, "requirements are limited to %d characters" % self.MAX_REQUIREMENTS_CHARS
        if project_id != BUILTIN and not self.exists(project_id):
            return False, "unknown project"
        path = self.requirements_path(project_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(text.encode("utf-8"))      # as written, line endings included
            tmp.replace(path)
        return True, None

    def touch(self, project_id):
        """Record activity so the newest work sorts and shows correctly."""
        if project_id == BUILTIN:
            return
        with self._lock:                    # read and write as one step: a rename in between stays
            cur = self.get(project_id) if self.exists(project_id) else None
            if cur:
                self._save(project_id, {"name": cur["name"], "description": cur["description"],
                                        "created": cur["created"],
                                        "updated": round(time.time(), 3)})

    def delete(self, project_id):
        """``(ok, error)``. Moves the folder to ``.trash``; nothing is erased."""
        if project_id == BUILTIN:
            return False, "the built-in project cannot be deleted"
        if not self.exists(project_id):
            return False, "unknown project"
        with self._lock:
            trash = self.root / ".trash"
            trash.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.root / project_id),
                        str(trash / ("%s-%d" % (project_id, int(time.time())))))
        return True, None
