"""Ouvrir KRT une seule fois, dans l'éditeur de la carte explicitement préparée."""
from pathlib import Path
import importlib
import json
import os
import sys
import time

import pcbnew
import wx
from .workflow import find_krt

ENVIRONMENT_KEY = 'PASSERELLE_FUSION_KRT_BOARD'
_timer = None


def same_board(board, target):
    return bool(board and board.GetFileName() and
                os.path.normcase(str(Path(board.GetFileName()).resolve())) ==
                os.path.normcase(str(target.resolve())))


def show_krt():
    root = find_krt().resolve()
    # Réutiliser le point d'entrée natif : il lit la carte en mémoire, présente
    # les choix KRT et ne route qu'après validation de son propre dialogue.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    module = importlib.import_module('kicad_routing_plugin.action_plugin')
    if Path(module.__file__).resolve().parent.parent != root:
        raise ValueError('Un autre module KRT est chargé. Redémarrer KiCad avant de réessayer.')
    module.KiCadRoutingToolsPlugin().Run()


def schedule_requested_dialog():
    global _timer
    # La demande est réservée au processus enfant lancé par la passerelle.
    # La retirer évite de la transmettre aux sous-processus de KRT ou à un reload.
    requested = os.environ.pop(ENVIRONMENT_KEY, '')
    if not requested:
        return
    target = Path(requested)
    deadline = time.monotonic()+60

    def status(state, detail=''):
        target.with_suffix('.krt-dialog.json').write_text(
            json.dumps(dict(state=state, detail=detail, board=str(target), pid=os.getpid()),
                       ensure_ascii=False, indent=2), encoding='utf-8')

    def ready():
        global _timer
        _timer = None
        try:
            windows = wx.GetTopLevelWindows()
            # Le plugin est importé avant que KiCad ait fini de charger le PCB.
            # Attendre aussi la fermeture des éventuels dialogues de démarrage.
            if (same_board(pcbnew.GetBoard(), target) and any(w.IsShown() for w in windows)
                    and not any(isinstance(w, wx.Dialog) and w.IsModal() for w in windows)):
                status('dialogue_requested')
                show_krt()
                status('dialogue_closed')
            elif time.monotonic() < deadline:
                _timer = wx.CallLater(500, ready)
            else:
                status('timeout', 'Carte non chargée ou dialogue de démarrage ouvert.')
                wx.MessageBox('La carte n’est pas prête. Ouvrir KRT avec son bouton dans cet éditeur.',
                              'Passerelle Fusion ↔ KiCad')
        except Exception as error:
            status('error', str(error))
            wx.MessageBox('Impossible d’ouvrir KRT : '+str(error), 'Passerelle Fusion ↔ KiCad')

    _timer = wx.CallLater(500, ready)
