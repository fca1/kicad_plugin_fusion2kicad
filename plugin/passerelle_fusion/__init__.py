"""Enregistrement du plugin Python de l'éditeur PCB KiCad 10."""
from .action import PasserelleFusion
import wx

# Le moteur est aussi testé avec le Python KiCad autonome, sans application.
if wx.GetApp() is not None:
    PasserelleFusion().register()
    from .krt_dialog import schedule_requested_dialog
    schedule_requested_dialog()
