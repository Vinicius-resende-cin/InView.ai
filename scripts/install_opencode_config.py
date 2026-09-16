"""Sync this project's opencode.json + .opencode/skills into opencode's
global config directory (~/.config/opencode). See README's Implementation
notes for why this is needed.

Run this once after editing opencode.json or .opencode/skills/, and again
any time those change. Safe to re-run (idempotent merge).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GLOBAL_CONFIG_DIR = Path.home() / ".config" / "opencode"


def _merge_opencode_json() -> None:
    """Merge this project's opencode.json into the global one (plugins
    unioned; provider/agent blocks overridden by the project's own)."""
    project_path = PROJECT_ROOT / "opencode.json"
    project_config = json.loads(project_path.read_text(encoding="utf-8"))

    global_path = GLOBAL_CONFIG_DIR / "opencode.json"
    global_config = (
        json.loads(global_path.read_text(encoding="utf-8")) if global_path.exists() else {}
    )

    global_config.setdefault("$schema", project_config.get("$schema"))

    plugins = set(global_config.get("plugin", [])) | set(project_config.get("plugin", []))
    if plugins:
        global_config["plugin"] = sorted(plugins)

    for key in ("provider", "agent"):
        merged = {**global_config.get(key, {}), **project_config.get(key, {})}
        if merged:
            global_config[key] = merged

    GLOBAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    global_path.write_text(json.dumps(global_config, indent=2) + "\n", encoding="utf-8")
    print(f"Merged {project_path} -> {global_path}")


def _sync_skills() -> None:
    """Copy every skill under .opencode/skills/ into the global skills dir,
    replacing any existing copy."""
    project_skills = PROJECT_ROOT / ".opencode" / "skills"
    if not project_skills.is_dir():
        return
    global_skills = GLOBAL_CONFIG_DIR / "skills"
    global_skills.mkdir(parents=True, exist_ok=True)

    for skill_dir in project_skills.iterdir():
        if not skill_dir.is_dir():
            continue
        dest = global_skills / skill_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(skill_dir, dest)
        print(f"Copied {skill_dir} -> {dest}")


def main() -> int:
    """Merge opencode.json and sync skills into opencode's global config."""
    _merge_opencode_json()
    _sync_skills()
    print("\nRestart any running `opencode serve` for changes to take effect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
