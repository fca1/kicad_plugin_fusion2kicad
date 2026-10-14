"""Parcours automatique exécuté hors de l'éditeur, sur des copies uniquement."""
from pathlib import Path
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys

from .import_board import find_cli
from .service import create_from_brd
from .core.kicad_routage_vers_fusion import main as convert, parse, nodes, field, net


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def run_process(command, log, timeout=1800):
    print('Exécution : '+str(command[1:]), flush=True)
    with log.open('w', encoding='utf-8') as stream:
        result = subprocess.run([str(x) for x in command], stdout=stream, stderr=subprocess.STDOUT,
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return result.returncode


def find_krt():
    # Le paquet KRT complet fournit une CLI avec son manifeste Rust ; le paquet
    # Gloss embarqué ne fournit pas tous les fichiers nécessaires à cette CLI.
    path = Path(__file__).resolve().parent.parent/'com_github_drandyhaas_kicadroutingtools'
    if not (path/'py_router/route.py').is_file():
        raise ValueError('Installer le plugin KiCad Routing Tools complet pour le routage automatique.')
    return path


def expected_dimensions(report):
    classes = {row['class']:row['after'] for row in report['classes']}
    result = {row['pattern']:dict(classes[row['netclass']]) for row in report['assignments']}
    for name, values in report.get('net_widths', {}).items():
        result[name]['track_width'] = values['preferred']
    return result


def verify_krt_project(pcb, report, krt):
    sys.path.insert(0, str(krt/'py_router'))
    try:
        from list_nets import read_design_rules, resolve_net_class
        rules = read_design_rules(str(pcb))
        expected = expected_dimensions(report)
        resolved = None
        if report.get('net_widths'):
            from design_rules import DesignRules
            from kicad_parser import parse_kicad_pcb
            data = parse_kicad_pcb(str(pcb))
            resolved = DesignRules.from_project(data, str(pcb))
            net_ids = {n.name:nid for nid,n in data.nets.items()}
        errors = []
        for name, dimensions in expected.items():
            actual = rules['classes'].get(resolve_net_class(name, rules), {})
            actual = dict(actual)
            if resolved is not None and name in report['net_widths']:
                nid = net_ids[name]
                actual['track_width'] = resolved.draw_size('track_width', nid)
                constraint = resolved.resolve('track_width', resolved.item_for_net(nid,'track'))
                if constraint.min is None or abs(constraint.min-report['net_widths'][name]['minimum'])>1e-6:
                    errors.append(f'{name}: minimum de largeur incohérent dans KRT')
            for key, value in dimensions.items():
                if not isinstance(actual.get(key), (int, float)) or abs(actual[key]-value)>1e-6:
                    errors.append(f'{name}: {key} attendu {value}, lu par KRT {actual.get(key)}')
        if errors:
            raise ValueError('Règles KRT incohérentes : '+' ; '.join(errors[:8]))
        return {'nets':len(expected), 'dimensions_par_net':4, 'concordants':True}
    finally:
        sys.path.remove(str(krt/'py_router'))


def audit_dimensions(pcb, report):
    expected = expected_dimensions(report)
    data = parse(pcb.read_text(encoding='utf-8-sig'))
    errors, counts = [], {'segments':0, 'vias':0}
    for kind in ('segment', 'arc', 'via'):
        for obj in nodes(data, kind):
            name = net(obj)
            if name not in expected:
                errors.append({'net':name, 'erreur':'Net cuivre sans règle Fusion'})
                continue
            keys = [('size','via_diameter'), ('drill','via_drill')] if kind=='via' else [('width','track_width')]
            counts['vias' if kind=='via' else 'segments'] += 1
            for key, rule in keys:
                value = float(field(obj, key)[0]); required = expected[name][rule]
                if rule == 'track_width' and name in report.get('net_widths', {}):
                    required = report['net_widths'][name]['minimum']
                if value+1e-6 < required:
                    errors.append({'net':name, 'objet':kind, 'dimension':rule,
                                   'mesuree_mm':value, 'requise_mm':required})
    return dict(counts, conforme=not errors, ecarts=errors)


def drc(pcbnew, pcb, output):
    code = run_process([find_cli(pcbnew), 'pcb', 'drc', '--format', 'json',
                        '--refill-zones', '--exit-code-violations', '-o', output, pcb],
                       output.with_suffix('.log'), 180)
    if code not in (0,5) or not output.is_file():
        raise ValueError('Échec du DRC KiCad ; consulter '+str(output.with_suffix('.log')))
    report = json.loads(output.read_text(encoding='utf-8'))
    errors = [x for key in ('violations','unconnected_items','schematic_parity')
              for x in report.get(key,[]) if x.get('severity')=='error']
    return dict(erreurs=len(errors), violations=len(report.get('violations', [])),
                non_connectes=len(report.get('unconnected_items', [])), rapport=str(output))


def validate_source(brd):
    path = Path(brd)
    if not path.is_file() or path.suffix.lower() not in ('.brd', '.fbrd'):
        raise ValueError('Sélectionner un fichier source Fusion .brd ou .fbrd.')
    return path


def prepare_for_krt(pcbnew, job, brd=''):
    # L'étape utilisateur ne lance jamais la CLI : seul le dialogue natif KRT
    # décidera du routage, après chargement de la copie dans son propre éditeur.
    validate_source(brd)
    find_krt()
    result = prepare_and_route(pcbnew, job, brd, route=False)
    result['open_krt'] = True
    folder = Path(result['target']).parent
    (folder/'README.txt').write_text(result['message'], encoding='utf-8')
    result['message'] = ('Carte et règles préparées.\n'
                         'Choisissez les paramètres et lancez le routage dans KRT.\n'
                         'Rapports : '+str(folder))
    return result


def prepare_and_route(pcbnew, job, brd='', route=False):
    brd = validate_source(brd)
    print('Import BRD et transfert des règles…', flush=True)
    project, message = create_from_brd(pcbnew, brd, job)
    board = project.with_suffix('.kicad_pcb')
    report = json.loads((project.parent/'rapport_regles.json').read_text(encoding='utf-8'))
    metadata = {'brd':str((project.parent.parent/'sources/original.brd').resolve()),
                'source_input':str(brd.resolve())}
    brd = Path(metadata['brd'])
    write_json(project.with_suffix('.fusion.json'), metadata)
    if not route:
        return dict(ok=True, target=str(project), message=message, open_board=True)
    krt = find_krt()
    write_json(job/'controle_avant.json', verify_krt_project(board, report, krt))
    baseline = drc(pcbnew, board, job/'drc_avant.json')
    output = project.parent/'routage'; output.mkdir()
    routed = output/board.name
    for ext in ('.kicad_pro', '.kicad_dru', '.fusion.json'):
        shutil.copy2(project.with_suffix(ext), routed.with_suffix(ext))
    shutil.copy2(project.parent/'rapport_regles.json', output/'rapport_regles.json')
    command = [sys.executable, krt/'py_router/route.py', board, routed,
               '--escalation', 'off', '--strict-sizes', '--no-power-tap-neckdown',
               '--json-out', job/'rapport_krt.json']
    print('Routage KRT : classes conservées, réduction automatique désactivée…', flush=True)
    code = run_process(command, job/'routage.log')
    if not routed.is_file():
        raise ValueError('KRT n’a pas produit de carte ; consulter '+str(job/'routage.log'))
    widths = audit_dimensions(routed, report)
    write_json(job/'controle_dimensions.json', widths)
    # Le routeur écrit aussi un projet : vérifier ce que le prochain lancement lira.
    verify_krt_project(routed, report, krt)
    after = drc(pcbnew, routed, job/'drc_apres.json')
    with contextlib.redirect_stdout(io.StringIO()):
        convert([str(routed),str(brd)])  # implantation et bijection de tous les contacts
    valid = code==0 and widths['conforme']
    message = (f"Routage terminé : {widths['segments']} segments, {widths['vias']} vias.\n"
               f"Dimensions {'conformes' if valid else 'NON validées'} ; DRC KiCad : {after['erreurs']} erreurs, "
               f"{after['non_connectes']} non-connectés.\nRapports : {job}\n")
    if not valid:
        message += 'Résultat ouvert pour diagnostic ; application automatique refusée si dimensions non conformes.'
    return dict(ok=True, target=str(routed.with_suffix('.kicad_pro')), message=message,
                open_board=True, dimensions_valides=valid, drc_avant=baseline, drc_apres=after)


def open_board(pcbnew, project, with_krt=False):
    board = Path(project).with_suffix('.kicad_pcb')
    editor = find_cli(pcbnew).with_name('pcbnew.exe' if sys.platform=='win32' else 'pcbnew')
    if not board.is_file() or not editor.is_file():
        raise ValueError('Carte ou éditeur introuvable pour l’ouverture automatique.')
    # Un processus indépendant charge le projet frère de la carte et conserve
    # l'éditeur d'origine, même si son document n'est pas enregistré.
    environment = os.environ.copy()
    environment.pop('PASSERELLE_FUSION_KRT_BOARD', None)
    if with_krt:
        environment['PASSERELLE_FUSION_KRT_BOARD'] = str(board.resolve())
    return subprocess.Popen([str(editor), str(board)], env=environment)
