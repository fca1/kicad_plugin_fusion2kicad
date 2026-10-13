"""Compagnon local. Toute API Fusion s'exécute sur son thread principal."""
from pathlib import Path
import hashlib
import json
import os
import threading
import time
import uuid
import adsk.core
import adsk.electron

EVENT = 'PasserelleKiCad.local.queue.v1'
ROOT = Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'PasserelleFusion'
_handlers = []
_pending = {}
_stop = None
_thread = None
_session = None


def write_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def active():
    app = adsk.core.Application.get()
    doc = adsk.electron.BoardDocument.cast(app.activeDocument)
    if not doc:
        raise ValueError('Activer le PCB cible dans Fusion, pas le schéma ou la vue 3D.')
    # Un identifiant de session couvre également les copies locales non sauvegardées.
    identity = doc.dataFile.id if doc.dataFile else str(doc.creationId)
    return app, doc, identity


def export(doc, path):
    if path.exists():
        raise ValueError('Export existant : aucune sauvegarde ne sera écrasée.')
    path.parent.mkdir(parents=True, exist_ok=True)
    manager = doc.board.exportManager
    if not manager.execute(manager.createEagleBrdExportOptions(str(path))):
        raise ValueError('Échec de l’export BRD Fusion.')


def command_path(path):
    value = str(path.resolve())
    if any(c in value for c in "'\";\r\n"):
        raise ValueError('Chemin incompatible avec la commande Fusion.')
    return "'"+value+"'"


def begin(message):
    app, doc, identity = active()
    if message['operation'] == 'export':
        path = Path(message['path']).resolve()
        export(doc, path)
        return dict(ok=True, identity=identity, name=doc.name, path=str(path), sha256=fingerprint(path))
    if message['operation'] != 'apply':
        raise ValueError('Opération Fusion inconnue.')
    if identity != message['identity']:
        raise ValueError('Le PCB actif n’est plus la cible contrôlée. Application refusée.')
    folder = Path(message['folder']).resolve()
    ulp = folder/'carte_fusion_pistes_vias.ulp'
    if fingerprint(ulp) != message['ulp_sha256']:
        raise ValueError('Le livrable ULP a changé après vérification.')
    # Export complet avant toute commande d'écriture, même si la cible est sale.
    backup = folder/'fusion_avant.brd'
    export(doc, backup)
    if fingerprint(backup) != message['source_sha256']:
        raise ValueError('Le contenu Fusion a changé depuis le contrôle. Repréparer le retour.')
    script = folder/'carte_fusion_pistes_vias.scr'
    if script.exists():
        raise ValueError('Dossier de retour déjà utilisé : préparer un nouveau retour.')
    app.executeTextCommand('Electron.run RUN '+command_path(ulp)+';')
    _pending[message['id']] = dict(message=message, phase='prepared', script=script,
                                   folder=folder, identity=identity, doc=doc)
    return None


def advance(item):
    message, folder = item['message'], item['folder']
    if time.time() > message['deadline']:
        raise ValueError('Délai Fusion dépassé ; état à contrôler, aucune relance automatique.')
    app, doc, identity = active()
    if identity != item['identity']:
        raise ValueError('La cible active a changé pendant l’opération.')
    marker = folder/'application_terminee.txt'
    if item['phase'] == 'prepared':
        if not item['script'].is_file():
            return None
        # Ce RUN n'arrive qu'après la totalité du script et le DRC. Sa présence
        # évite de confondre commande acceptée et application réellement terminée.
        marker_ulp = folder/'fin_application.ulp'
        marker_ulp.write_text('output('+json.dumps(str(marker)) + ', "wt") { printf("ok"); }\n', encoding='utf-8')
        script = folder/'appliquer_avec_drc.scr'
        script.write_text(item['script'].read_text(encoding='utf-8')+
                          '\nDRC;\nRUN '+command_path(marker_ulp)+';\n', encoding='utf-8')
        item['phase'] = 'applied'
        app.executeTextCommand('Electron.run SCRIPT '+command_path(script)+';')
        return None
    if not marker.is_file():
        return None
    after = folder/'fusion_apres.brd'
    export(doc, after)
    errors = [dict(code=e.code, description=e.description, state=int(e.state),
                   type=int(e.errorType)) for e in doc.board.errors]
    report = dict(ok=True, applied=True, identity=identity, name=doc.name,
                  backup=str(folder/'fusion_avant.brd'), after=str(after),
                  drc_checked=bool(doc.board.checked), errors=errors,
                  saved=False)
    write_json(folder/'drc_fusion.json', report)
    return report


class Handler(adsk.core.CustomEventHandler):
    def notify(self, args):
        folder = ROOT/_session['id']
        for path in list(folder.glob('*.request')):
            message = None
            try:
                message = json.loads(path.read_text(encoding='utf-8'))
                path.rename(path.with_suffix('.consumed'))
                if message['token'] != _session['token'] or time.time() > message['deadline']:
                    raise ValueError('Requête expirée ou session périmée.')
                if _pending:
                    raise ValueError('Une application Fusion est déjà en cours.')
                result = begin(message)
                if result is not None:
                    write_json(path.with_suffix('.response'), result)
            except Exception as error:
                write_json(path.with_suffix('.response'), dict(ok=False, error=str(error)))
        for ident, item in list(_pending.items()):
            try:
                result = advance(item)
            except Exception as error:
                result = dict(ok=False, error=str(error))
            if result is not None:
                write_json(folder/(ident+'.response'), result)
                del _pending[ident]


def run(context):
    global _stop, _session, _thread
    if _stop is not None:
        return
    app = adsk.core.Application.get()
    ROOT.mkdir(parents=True, exist_ok=True)
    _session = dict(id=uuid.uuid4().hex, token=uuid.uuid4().hex, heartbeat=time.time())
    (ROOT/_session['id']).mkdir()
    event = app.registerCustomEvent(EVENT)
    handler = Handler(); event.add(handler); _handlers.append(handler)
    _stop = threading.Event()
    stop_event = _stop
    def poll():
        # Garder l'événement de cette exécution : stop() remet le global à None.
        while not stop_event.wait(.5):
            _session['heartbeat'] = time.time()
            write_json(ROOT/'session.json', _session)
            app.fireCustomEvent(EVENT)
    _thread = threading.Thread(target=poll, daemon=True)
    _thread.start()


def stop(context):
    global _stop
    if _stop:
        _stop.set()
        _thread.join(timeout=2)
        adsk.core.Application.get().unregisterCustomEvent(EVENT)
        _handlers.clear()
        _stop = None
