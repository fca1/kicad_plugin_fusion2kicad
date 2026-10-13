"""Échanges locaux avec l'add-in Fusion, sans port réseau ni exécution arbitraire."""
from pathlib import Path
import json
import os
import time
import uuid


def bridge_root():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home())))/'PasserelleFusion'


def request(operation, payload, timeout=180):
    root = bridge_root()
    try:
        session = json.loads((root/'session.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError('Démarrer l’add-in PasserelleKiCad dans Fusion (Scripts et compléments).') from error
    if time.time()-session['heartbeat'] > 10:
        raise ValueError('La connexion Fusion est inactive : démarrer PasserelleKiCad.')
    ident = uuid.uuid4().hex
    folder = root/session['id']; folder.mkdir(exist_ok=True)
    message = dict(payload, operation=operation, token=session['token'],
                   deadline=time.time()+timeout, id=ident)
    temp = folder/(ident+'.tmp'); target = folder/(ident+'.request')
    temp.write_text(json.dumps(message, ensure_ascii=False), encoding='utf-8')
    temp.replace(target)
    response = folder/(ident+'.response')
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if response.is_file():
            result = json.loads(response.read_text(encoding='utf-8'))
            if not result.get('ok'):
                raise ValueError(result.get('error', 'Échec Fusion'))
            return result
        time.sleep(.2)
    raise ValueError('Fusion n’a pas confirmé la fin de l’opération. Ne pas relancer sans vérifier son état.')
