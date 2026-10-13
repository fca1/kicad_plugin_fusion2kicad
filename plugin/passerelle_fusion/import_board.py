"""Import EAGLE isolé : ne jamais remplacer la carte ouverte dans l'éditeur."""
from pathlib import Path
import json
import re

from .core.kicad_routage_vers_fusion import parse, nodes, field
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def find_cli(pcbnew):
    name = 'kicad-cli.exe' if sys.platform == 'win32' else 'kicad-cli'
    # Le binaire voisin de l'éditeur prime sur une autre version du PATH.
    candidates = [Path(sys.executable).with_name(name)]
    module = Path(pcbnew.__file__).resolve()
    candidates.extend(parent/name for parent in module.parents)
    found = shutil.which(name)
    if found:
        candidates.append(Path(found))
    for path in candidates:
        if path.is_file():
            return path
    raise ValueError('Import automatique indisponible : kicad-cli de KiCad 10 introuvable.')


def import_board(pcbnew, brd, inputs):
    """Créer une carte et un projet neuf, avec rapports conservés en cas d'échec."""
    root = ET.parse(brd).find('./drawing/board')
    if root is None or not root.findall('./elements/element'):
        raise ValueError('Sélectionner un BRD XML EAGLE contenant une carte et des composants.')
    cli = find_cli(pcbnew)
    options = dict(capture_output=True, text=True, encoding='utf-8', errors='replace',
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    version = subprocess.run([str(cli), '--version'], timeout=15, **options)
    if version.returncode or not version.stdout.strip().startswith('10.'):
        raise ValueError('L’import automatique nécessite kicad-cli version 10.')
    snapshot = inputs/'carte.kicad_pcb'
    report_path = inputs/'rapport_import.json'
    command = [str(cli), 'pcb', 'import', '--format', 'eagle', '--report-format', 'json',
               '--report-file', str(report_path), '-o', str(snapshot), str(brd)]
    try:
        result = subprocess.run(command, timeout=180, **options)
    except subprocess.TimeoutExpired as error:
        raise ValueError('Import BRD interrompu après 180 secondes ; aucun résultat validé.') from error
    (inputs/'import.log').write_text(result.stdout+'\n'+result.stderr, encoding='utf-8')
    if result.returncode or not snapshot.is_file() or not report_path.is_file():
        raise ValueError(f'Import BRD échoué. Consulter {inputs / "import.log"}')
    report = json.loads(report_path.read_text(encoding='utf-8-sig'))
    if report.get('errors'):
        raise ValueError(f'L’importeur signale des erreurs. Consulter {report_path}')

    # EAGLE accepte "G", KiCad l'annote "G0". Rétablir le nom source seulement
    # si l'origine et la position prouvent cette transformation particulière.
    elements = {e.get('name'):e for e in root.findall('./elements/element')}
    content = snapshot.read_text(encoding='utf-8')
    parsed = parse(content)
    footprints = {next((p[2] for p in nodes(fp, 'property') if p[1] == 'Reference'), ''):fp
                  for fp in nodes(parsed, 'footprint')}
    renames = []
    for name, element in elements.items():
        if name in footprints:
            continue
        candidate = footprints.get(name+'0') if name+'0' not in elements else None
        if candidate is None:
            raise ValueError('Référence source non retrouvée après import : '+name)
        pos = field(candidate, 'at')
        if abs(float(pos[0])-float(element.get('x')))>1e-5 or abs(float(pos[1])+float(element.get('y')))>1e-5:
            raise ValueError('Position incohérente pour la référence réannotée : '+name)
        renames.append({'imported':name+'0', 'source':name})
        pattern = r'(\(property\s+"Reference"\s+)' + re.escape(json.dumps(name+'0', ensure_ascii=False))
        content, count = re.subn(pattern, lambda m:m[1]+json.dumps(name,ensure_ascii=False), content)
        if count != 1:
            raise ValueError('Référence réannotée ambiguë : '+name)
    if renames:
        snapshot.write_text(content, encoding='utf-8')
        report['reference_restorations'] = renames
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')

    # Un gestionnaire indépendant fournit les valeurs natives de KiCad sans
    # hériter du projet actuellement ouvert ni changer le projet actif.
    project_path = inputs/'carte.kicad_pro'
    manager = pcbnew.SETTINGS_MANAGER()
    manager.LoadProject(str(project_path), False)  # False attendu pour un fichier neuf.
    project = manager.GetProject(str(project_path))
    try:
        if not manager.SaveProject(str(project_path), project) or not project_path.is_file():
            raise ValueError('KiCad n’a pas créé le nouveau projet.')
    finally:
        manager.UnloadProject(project, False)
    return report
