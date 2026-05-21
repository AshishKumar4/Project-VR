"""Save/load configuration profiles as JSON."""
import json
from pathlib import Path

PROFILES_DIR = Path.home() / "g2-studio/profiles"
PROFILES_DIR.mkdir(parents=True, exist_ok=True)


def list_profiles():
    return sorted([p.stem for p in PROFILES_DIR.glob("*.json")])


def load(name: str) -> dict:
    p = PROFILES_DIR / f"{name}.json"
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def save(name: str, config: dict):
    p = PROFILES_DIR / f"{name}.json"
    with open(p, "w") as f:
        json.dump(config, f, indent=2)
    return str(p)


def delete(name: str):
    p = PROFILES_DIR / f"{name}.json"
    if p.exists():
        p.unlink()
        return True
    return False
