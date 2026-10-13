"""Interface native : carte courante et projet capturés directement dans KiCad."""
from pathlib import Path
import traceback
import json
import subprocess
import uuid
import pcbnew
import wx
from .service import run, run_command
from .workflow import open_board
from .import_board import find_cli


PREPARE_KRT = 0
PREPARE_RETURN = 1


class BridgeDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title='Passerelle Fusion ↔ KiCad', size=(700,590),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.target = None
        self.process = None
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.board = pcbnew.GetBoard()
        name = self.board.GetFileName() if self.board else ''
        current = wx.StaticText(panel,label='Carte : '+(Path(name).name if name else '(aucune)'))
        current.SetToolTip(name)
        layout.Add(current,0,wx.EXPAND|wx.ALL,8)
        layout.Add(wx.StaticText(panel,label='Source Fusion (.brd / .fbrd) — vide : PCB actif'),0,wx.LEFT|wx.TOP,8)
        self.brd = wx.FilePickerCtrl(panel,message='Choisir la carte Fusion',wildcard='Cartes Fusion (*.brd;*.fbrd)|*.brd;*.fbrd',style=wx.FLP_OPEN|wx.FLP_FILE_MUST_EXIST|wx.FLP_USE_TEXTCTRL)
        layout.Add(self.brd,0,wx.EXPAND|wx.ALL,10)
        self.mode = wx.RadioBox(panel,label='Opération',choices=[
            'Préparer la carte et ouvrir le dialogue KRT',
            'Préparer le retour des pistes et vias vers Fusion'],majorDimension=1,style=wx.RA_SPECIFY_COLS)
        layout.Add(self.mode,0,wx.EXPAND|wx.ALL,10)
        layout.Add(wx.StaticText(panel,label='Dossier des résultats'),0,wx.LEFT,10)
        self.folder = wx.DirPickerCtrl(panel,path=str(Path(name).parent if name else Path.home()),style=wx.DIRP_USE_TEXTCTRL)
        layout.Add(self.folder,0,wx.EXPAND|wx.ALL,10)
        hint=wx.StaticText(panel,label='KRT : choisissez les paramètres dans le nouvel éditeur.\nSans fichier source : activez PasserelleKiCad dans Fusion.')
        layout.Add(hint,0,wx.ALL,10)
        self.result = wx.TextCtrl(panel,style=wx.TE_MULTILINE|wx.TE_READONLY)
        layout.Add(self.result,1,wx.EXPAND|wx.ALL,10)
        layout.Add(wx.StaticText(panel,label='Commande Fusion'),0,wx.LEFT,10)
        self.command = wx.TextCtrl(panel,style=wx.TE_READONLY)
        layout.Add(self.command,0,wx.EXPAND|wx.ALL,10)
        self.copy = wx.Button(panel,label='Copier la commande RUN');self.copy.Disable()
        layout.Add(self.copy,0,wx.LEFT|wx.BOTTOM,10)
        self.copy.Bind(wx.EVT_BUTTON,self.copy_command)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.go=wx.Button(panel,label='Préparer')
        self.open=wx.Button(panel,label='Ouvrir le résultat');self.open.Disable()
        self.details=wx.Button(panel,label='Détails');self.details.Disable()
        close=wx.Button(panel,wx.ID_CLOSE,label='Fermer')
        for button in (self.go,self.details,self.open,close): buttons.Add(button,0,wx.ALL,5)
        layout.Add(buttons,0,wx.ALIGN_RIGHT|wx.ALL,5)
        panel.SetSizer(layout)
        outer=wx.BoxSizer(wx.VERTICAL);outer.Add(panel,1,wx.EXPAND);self.SetSizer(outer)
        self.go.Bind(wx.EVT_BUTTON,self.prepare)
        self.open.Bind(wx.EVT_BUTTON,self.open_result)
        self.details.Bind(wx.EVT_BUTTON,self.open_details)
        self.timer=wx.Timer(self)
        self.Bind(wx.EVT_TIMER,self.poll_worker,self.timer)
        self.Bind(wx.EVT_CLOSE,self.on_close)
        self.close_button=close
        close.Bind(wx.EVT_BUTTON,self.on_close)
        self.CentreOnParent()

    def open_details(self,event=None):
        if self.target:
            folder = self.target.parent if self.target.suffix=='.kicad_pro' else self.target
            notice = folder/'README.txt'
            wx.LaunchDefaultApplication(str(notice if notice.is_file() else folder/'README.md'))

    def on_close(self,event):
        if self.process is not None:
            if isinstance(event,wx.CloseEvent): event.Veto()
            return
        self.EndModal(wx.ID_CLOSE)

    def open_result(self,event=None):
        if self.target and self.target.suffix=='.kicad_pro':
            open_board(pcbnew,self.target)
        elif self.target:
            wx.LaunchDefaultApplication(str(self.target))

    def start_worker(self):
        job=Path(self.folder.GetPath()).resolve()/('automatisation_'+uuid.uuid4().hex[:12])
        job.mkdir(parents=True)
        config=dict(operation='prepare',brd=self.brd.GetPath())
        config_path=job/'request.json'
        config_path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8')
        python=find_cli(pcbnew).with_name('python.exe')
        if not python.is_file(): raise ValueError('Python KiCad introuvable.')
        self.log=(job/'execution.log').open('w',encoding='utf-8')
        self.job=job
        self.process=subprocess.Popen([str(python),'-B','-u','-X','utf8',str(Path(__file__).with_name('worker.py')),str(config_path)],
            stdout=self.log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for control in (self.go,self.mode,self.brd,self.folder,self.close_button): control.Disable()
        self.result.SetValue('Opération en cours. Rapports : '+str(job))
        self.timer.Start(500)

    def poll_worker(self,event):
        if self.process.poll() is None:
            try:
                text=(self.job/'execution.log').read_text(encoding='utf-8',errors='replace')
                self.result.SetValue(text[-6000:] or 'Opération en cours…')
            except OSError: pass
            return
        self.timer.Stop();self.log.close();self.process=None
        for control in (self.go,self.mode,self.brd,self.folder,self.close_button): control.Enable()
        try:
            result=json.loads((self.job/'result.json').read_text(encoding='utf-8'))
            self.result.SetValue(result['message'])
            if result['ok']:
                self.target=Path(result['target']);self.open.Enable();self.details.Enable()
                if result.get('open_board'):
                    open_board(pcbnew,self.target,with_krt=result.get('open_krt',False))
        except Exception as error:
            self.result.AppendText('\n'+str(error)+'\nConsulter '+str(self.job/'execution.log'))

    def prepare(self,event):
        self.go.Disable();self.open.Disable();self.details.Disable();self.target=None
        self.command.Clear();self.copy.Disable()
        try:
            if self.mode.GetSelection()==PREPARE_KRT:
                self.start_worker()
                return
            if self.mode.GetSelection()!=PREPARE_RETURN:
                raise ValueError('Opération inconnue.')
            # Ce parcours prépare seulement le livrable ; l’utilisateur lance
            # explicitement la commande RUN dans Fusion après sa préparation.
            with wx.BusyCursor():
                self.target,message=run(pcbnew,self.board,self.brd.GetPath(),self.folder.GetPath(),False)
            self.result.SetValue(message);self.open.Enable();self.details.Enable()
            self.command.SetValue(run_command(self.target));self.copy.Enable()
        except Exception as error:
            self.result.SetValue(str(error))
            # Le diagnostic technique reste disponible dans la console KiCad.
            print(traceback.format_exc())
        finally:
            if self.process is None: self.go.Enable()

    def copy_command(self,event):
        # Ne remplacer le presse-papiers qu'à la demande explicite du bouton.
        if not self.command.GetValue(): return
        if not wx.TheClipboard.Open():
            wx.MessageBox('Presse-papiers occupé. Réessayer.', 'Copie impossible', parent=self)
            return
        try:
            copied=wx.TheClipboard.SetData(wx.TextDataObject(self.command.GetValue()))
            if copied: wx.TheClipboard.Flush()
        finally:
            wx.TheClipboard.Close()
        if not copied:
            wx.MessageBox('La commande n’a pas pu être copiée.', 'Copie impossible', parent=self)


class PasserelleFusion(pcbnew.ActionPlugin):
    def defaults(self):
        self.name='Passerelle Fusion ↔ KiCad'
        self.category='Échange de cartes'
        self.description='Préparation KRT et retour du routage vers Fusion'
        self.show_toolbar_button=True
        self.icon_file_name=str(Path(__file__).with_name('icon.png'))

    def Run(self):
        parent=next((w for w in wx.GetTopLevelWindows() if 'pcb' in w.GetName().lower()),None)
        dialog=BridgeDialog(parent)
        try: dialog.ShowModal()
        finally: dialog.Destroy()
