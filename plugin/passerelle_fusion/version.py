"""Version figée à l'assemblage : aucun accès Git depuis KiCad."""
import json
from pathlib import Path


def display_version():
    path = Path(__file__).with_name('_version.json')
    if not path.exists():
        return 'développement (non assemblé)'
    data = json.loads(path.read_text(encoding='utf-8'))
    suffix = ' · modifié' if data['dirty'] else ''
    return f"r{data['revision']} · {data['commit'][:8]}{suffix}"
