"""User channel preferences, stored in data/prefs.json.

Records written before the November migration keep the prefs_v1 string ("email,sms").
Newer records store a channels list. The file still holds both kinds and nothing rewrites
the old ones.
"""

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "prefs.json"


def channels_for(user_id: str, path: Path = DATA) -> list[str]:
    """Channels the user accepts, for example ["email", "sms"]."""
    records = json.loads(path.read_text(encoding="utf-8"))
    for record in records:
        if record["user"] != user_id:
            continue
        if "channels" in record:
            return list(record["channels"])
        return [item for item in record["prefs_v1"].split(",") if item]
    return []
