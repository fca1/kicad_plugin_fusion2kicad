"""Instantané de la carte ouverte et opérations locales, sans serveur HTTP."""
from pathlib import Path
import contextlib
import io
import json
import shutil
import uuid

from .import_board import import_board
from .core.fusion_source import prepare_source
from .core.regles_fusion_kicad import transfer
from .core.kicad_routage_vers_fusion import main as convert
from .core.verifier_script_routage import check


def run_command(folder):
    """L'ULP déjà présent vérifie la cible puis génère et applique le SCR."""
    # Path choisit WindowsPath ou PosixPath selon l'OS du plugin.
    path = str((Path(folder)/'carte_fusion_pistes_vias.ulp').resolve())
    return "RUN '" + path.replace("'", "''") + "';"


def run(pcbnew, board, brd, destination, rules):
    brd = Path(brd).resolve()
    if not brd.is_file() or brd.suffix.lower() not in ('.brd', '.fbrd'):
        raise ValueError('Sélectionner la carte Fusion .brd ou .fbrd source.')
    if not board or not board.GetFileName():
        raise ValueError('Ouvrir une carte KiCad avec un nom de fichier.')
    if not pcbnew.GetBuildVersion().startswith('10.'):
        raise ValueError('Cette version du plugin nécessite KiCad 10.')
    if not any(board.GetFootprints()):
        raise ValueError('La carte KiCad ouverte est vide. Choisir « Préparer la carte et ouvrir le dialogue KRT ».')
    source = Path(board.GetFileName()).resolve()
    # Un dossier neuf conserve les instantanés audités sans écraser un projet.
    job = Path(destination).resolve()/('fusion_'+uuid.uuid4().hex[:12])
    inputs, output = job/'sources', job/'resultat'
    inputs.mkdir(parents=True)
    # Le dialogue conserve ce dossier même si la préparation échoue : les
    # diagnostics ne doivent pas dépendre de l'existence d'un livrable validé.
    if hasattr(board,'GetFileName'):
        run.last_job = job
    _, source_report = prepare_source(brd, inputs)
    snapshot = inputs/'carte.kicad_pcb'
    # PCB_IO_MGR.Save sérialise l'état en mémoire sans SaveBoard (qui peut
    # changer le nom de la carte). L'original, même non enregistré, reste ouvert.
    pcbnew.PCB_IO_MGR.Save(pcbnew.PCB_IO_MGR.KICAD_SEXP, str(snapshot), board)
    if board.GetFileName() != str(source) and Path(board.GetFileName()).resolve() != source:
        raise RuntimeError('Le nom de la carte a changé pendant la capture.')
    if rules:
        if not source.with_suffix('.kicad_pro').is_file():
            raise ValueError('Enregistrer d’abord un projet KiCad (.kicad_pro) associé à cette carte.')
        project = inputs/'carte.kicad_pro'
        pcbnew.GetSettingsManager().SaveProjectCopy(str(project), board.GetProject())
        if not project.is_file():
            raise ValueError('KiCad n’a pas produit la copie de ses paramètres de projet.')
        dru = source.with_suffix('.kicad_dru')
        copied_dru = inputs/'carte.kicad_dru'
        if dru.exists(): shutil.copy2(dru, copied_dru)
        report = transfer(inputs/'original.brd', snapshot, project, output,
                          'carte_fusion', copied_dru if dru.exists() else None)
        target = output/'carte_fusion.kicad_pro'
        message = rules_message(report)
    else:
        # En préparation : le clic dans KiCad ne déclenche aucune action Fusion.
        with contextlib.redirect_stdout(io.StringIO()):
            convert([str(snapshot),str(inputs/'original.brd'),'--output',str(output),
                     '--name','carte_fusion'])
            check(output)
        report = json.loads((output/'rapport_conversion.json').read_text(encoding='utf-8'))
        target = output
        message = (f"{report['segments_kicad']} segments source, {report['vias_kicad']} vias, {report['zones_kicad']} zone(s) cuivre.\n"
                   f"{len(report['junctions'])} liaisons explicites contenues dans le cuivre.\n"
                   'Copier la commande RUN ci-dessous et la valider dans le PCB cible ouvert dans Fusion.\n'
                   'Elle vérifie la carte, génère le script et applique les pistes, vias et zones prises en charge.\n'
                   'Le routage des nets concernés est remplacé. Contrôler ensuite le DRC Fusion.\n'
                   'Les contacts non prouvés restent inchangés. Application 4/6 couches non prise en charge.')
        if report['zones_kicad']:
            message += ('\nChaque zone conserve son contour, son net, son isolation et ses connexions directes. '
                        'Une seule zone par couche est acceptée ; sa priorité est normalisée sans concurrence sur cette couche. '
                        'Fusion recalcule le cuivre avec ses règles : vérifier le remplissage et le DRC. '
                        'Les rectangles source sont remplacés après contrôle du net, de la couche et du contour ; '
                        'tout polygone cible inattendu est refusé. Ne pas lancer le fichier .body.scr directement.')
        # Conserver la notice complète sans la répéter dans le petit dialogue.
        warnings = report.get('warnings', []) + source_report['warnings']
        warning_text = '\n'.join('Avertissement : '+warning for warning in warnings)
        (output/'README.txt').write_text(message+'\n'+warning_text, encoding='utf-8')
        message = (f"Prêt : {report['segments_kicad']} segments, {report['vias_kicad']} vias, {report['zones_kicad']} zone(s).\n"
                   'Copiez la commande RUN dans le PCB cible Fusion.\n'
                   'Le routage concerné sera remplacé. Vérifiez le remplissage et le DRC.')
        unresolved = len(report.get('unresolved_junctions', []))
        if unresolved:
            message += f'\nAttention : {unresolved} contact(s) non résolu(s), laissés inchangés.'
        if warnings:
            message += '\nRapports : '+str(output)
    (job/'capture.json').write_text(json.dumps({'carte_ouverte':str(source),
        'kicad':pcbnew.GetBuildVersion(),'snapshot_inclut_modifications_non_enregistrees':True,
        'source_originale_enregistree':False},ensure_ascii=False,indent=2),encoding='utf-8')
    return target, message+ ('\n'+'\n'.join(source_report['warnings']) if rules else '')


def rules_message(report):
    message = (f"{len(report['classes'])} classes, {len(report['assignments'])} nets.\n"
               'Ouvrir le résultat puis sa carte avant de router dans KRT.\n'
               'Les règles sont dans ce nouveau projet ; la carte actuellement ouverte reste inchangée.\n'
               'Les règles non traduites et les majorations sont dans le rapport.\n')
    message += '\n'.join(f"{c['class']} : piste {c['after']['track_width']:g} mm ; clearance {c['after']['clearance']:g} mm" for c in report['classes'])+'\n\n'
    if report['modern_rules']['rules']:
        message += 'Minima DRC des pistes : '+', '.join(
            f"{c['class']} {c['track_width_minimum']:g} mm" for c in report['classes'])+'\n'
        message += 'Transfert moderne partiel : consulter le rapport et les avertissements.\n' if not report['modern_rules']['complete'] else 'Règles modernes présentes traitées ; autres limites dans le rapport.\n'
    message += '\n'.join(report.get('warnings', []))
    return message


def create_from_brd(pcbnew, brd, destination):
    """Parcours autonome autorisé : BRD -> carte -> projet -> règles."""
    brd = Path(brd).resolve()
    if not brd.is_file() or brd.suffix.lower() not in ('.brd', '.fbrd'):
        raise ValueError('Sélectionner la carte Fusion .brd ou .fbrd source.')
    if not pcbnew.GetBuildVersion().startswith('10.'):
        raise ValueError('Cette version du plugin nécessite KiCad 10.')
    job = Path(destination).resolve()/('fusion_'+uuid.uuid4().hex[:12])
    inputs, output = job/'sources', job/'resultat'
    inputs.mkdir(parents=True)
    source, source_report = prepare_source(brd, inputs)
    imported = import_board(pcbnew, source, inputs)
    report = transfer(source, inputs/'carte.kicad_pcb', inputs/'carte.kicad_pro',
                      output, 'carte_fusion')
    shutil.copy2(inputs/'rapport_import.json', output/'rapport_import.json')
    shutil.copy2(inputs/'rapport_source.json', output/'rapport_source.json')
    (job/'capture.json').write_text(json.dumps({
        'operation': 'creation_depuis_brd', 'brd_source': str(brd),
        'kicad': pcbnew.GetBuildVersion(), 'carte_ouverte_modifiee': False,
        'import': imported}, ensure_ascii=False, indent=2), encoding='utf-8')
    message = 'Projet créé depuis le BRD Fusion.\n'+rules_message(report)
    message += '\n'+'\n'.join(source_report['warnings'])
    message += '\nRapport d’import : '+str(output/'rapport_import.json')
    for warning in imported.get('warnings', []):
        message += '\nAvertissement import : '+str(warning)
    if imported.get('statistics', {}).get('zones', 0):
        message += ('\nZones importées : vérifier et recalculer leur remplissage dans KiCad. '
                    'Les paramètres des zones ne sont pas corrigés par le transfert des règles.')
    return output/'carte_fusion.kicad_pro', message
