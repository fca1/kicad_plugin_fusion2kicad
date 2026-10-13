"""Processus de travail : aucune API de l'éditeur KiCad ouvert n'est appelée."""
from pathlib import Path
import json
import sys
import traceback

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import pcbnew
    from passerelle_fusion.workflow import prepare_for_krt, apply_to_fusion
    path = Path(sys.argv[1]).resolve()
    config = json.loads(path.read_text(encoding='utf-8'))
    try:
        if config['operation']=='prepare':
            result = prepare_for_krt(pcbnew, path.parent, config.get('brd',''))
        elif config['operation']=='apply':
            result = apply_to_fusion(pcbnew, path.parent, Path(config['snapshot']))
        else:
            raise ValueError('Opération inconnue.')
    except Exception as error:
        traceback.print_exc()
        result = dict(ok=False, message=str(error))
    (path.parent/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
