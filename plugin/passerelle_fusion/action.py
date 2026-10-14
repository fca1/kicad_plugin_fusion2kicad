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
from .version import display_version


# La sélection appartient à la session de cet éditeur, pas à une carte ni à
# une configuration persistante. Un nouveau processus repart donc vide.
_session_source = ''
_session_folder = ''


class BridgeDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title='Passerelle Fusion ↔ KiCad — '+display_version(), size=(740,700),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.target = None
        self.diagnostic_folder = None
        self.process = None
        panel = wx.Panel(self)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.board = pcbnew.GetBoard()
        name = self.board.GetFileName() if self.board else ''
        current = wx.StaticText(panel,label='Carte : '+(Path(name).name if name else '(aucune)'))
        current.SetToolTip(name)
        layout.Add(current,0,wx.EXPAND|wx.ALL,8)
        layout.Add(wx.StaticText(panel,label='1. Source Fusion (.brd ou .fbrd obligatoire)'),0,wx.LEFT|wx.TOP,10)
        self.brd = wx.FilePickerCtrl(panel,path=_session_source,message='Choisir la carte Fusion',wildcard='Cartes Fusion (*.brd;*.fbrd)|*.brd;*.fbrd',style=wx.FLP_OPEN|wx.FLP_FILE_MUST_EXIST|wx.FLP_USE_TEXTCTRL)
        layout.Add(self.brd,0,wx.EXPAND|wx.ALL,10)
        self.go=wx.Button(panel,label='2. Préparer vers KiCad — ouvrir KRT')
        layout.Add(self.go,0,wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP,10)
        layout.Add(wx.StaticText(panel,label='Importer la source dans une copie, puis choisir le routage dans KRT.'),0,wx.LEFT|wx.BOTTOM,10)
        self.return_button=wx.Button(panel,label='3. Préparer depuis KiCad — générer le retour Fusion')
        layout.Add(self.return_button,0,wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP,10)
        layout.Add(wx.StaticText(panel,label='Utiliser la carte routée ouverte dans cet éditeur PCB.'),0,wx.LEFT|wx.BOTTOM,10)
        layout.Add(wx.StaticText(panel,label='4. Copier le RUN et le lancer dans le PCB cible Fusion'),0,wx.LEFT|wx.TOP,10)
        self.command = wx.TextCtrl(panel,style=wx.TE_READONLY)
        layout.Add(self.command,0,wx.EXPAND|wx.ALL,10)
        self.copy = wx.Button(panel,label='Copier la commande RUN');self.copy.Disable()
        layout.Add(self.copy,0,wx.LEFT|wx.BOTTOM,10)
        self.copy.Bind(wx.EVT_BUTTON,self.copy_command)
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.open=wx.Button(panel,label='Ouvrir le résultat');self.open.Disable()
        close=wx.Button(panel,wx.ID_CLOSE,label='Fermer')
        for button in (self.open,close): buttons.Add(button,0,wx.ALL,5)
        layout.Add(buttons,0,wx.ALIGN_RIGHT|wx.LEFT|wx.RIGHT,10)
        layout.Add(wx.StaticText(panel,label='Dossier des résultats'),0,wx.LEFT|wx.TOP,10)
        initial_folder = _session_folder if _session_source else ''
        if _session_source and not initial_folder:
            initial_folder = str(Path(_session_source).parent)
        self.folder = wx.DirPickerCtrl(panel,path=initial_folder,style=wx.DIRP_USE_TEXTCTRL)
        self._suggested_folder = str(Path(_session_source).parent) if _session_source else ''
        layout.Add(self.folder,0,wx.EXPAND|wx.ALL,10)
        result_heading=wx.BoxSizer(wx.HORIZONTAL)
        result_heading.Add(wx.StaticText(panel,label='Résultat'),1,wx.ALIGN_CENTER_VERTICAL)
        self.copy_result=wx.Button(panel,label='Copier le résultat')
        result_heading.Add(self.copy_result,0)
        layout.Add(result_heading,0,wx.EXPAND|wx.LEFT|wx.RIGHT|wx.TOP,10)
        self.result = wx.TextCtrl(panel,style=wx.TE_MULTILINE|wx.TE_READONLY)
        layout.Add(self.result,1,wx.EXPAND|wx.ALL,10)
        self.copy_result.Bind(wx.EVT_BUTTON,self.copy_result_text)
        self.result.Bind(wx.EVT_TEXT,self.on_result_changed)
        panel.SetSizer(layout)
        outer=wx.BoxSizer(wx.VERTICAL);outer.Add(panel,1,wx.EXPAND);self.SetSizer(outer)
        self.SetMinSize((700,660))
        self.go.Bind(wx.EVT_BUTTON,lambda event: self.prepare(event,'to_kicad'))
        self.return_button.Bind(wx.EVT_BUTTON,lambda event: self.prepare(event,'from_kicad'))
        self.open.Bind(wx.EVT_BUTTON,self.open_result)
        self.timer=wx.Timer(self)
        self.Bind(wx.EVT_TIMER,self.poll_worker,self.timer)
        self.Bind(wx.EVT_CLOSE,self.on_close)
        self.close_button=close
        close.Bind(wx.EVT_BUTTON,self.on_close)
        self.brd.Bind(wx.EVT_FILEPICKER_CHANGED,self.on_source_changed)
        # Le champ texte permet aussi de saisir ou effacer directement le chemin.
        self.brd.GetTextCtrl().Bind(wx.EVT_TEXT,self.on_source_changed)
        self.folder.Bind(wx.EVT_DIRPICKER_CHANGED,self.on_folder_changed)
        self.folder.GetTextCtrl().Bind(wx.EVT_TEXT,self.on_folder_changed)
        self.update_actions()
        self.CentreOnParent()

    def on_close(self,event):
        if self.process is not None:
            if isinstance(event,wx.CloseEvent): event.Veto()
            return
        self.remember_source()
        self.EndModal(wx.ID_CLOSE)

    def remember_source(self):
        global _session_source, _session_folder
        # Mémoriser aussi le champ vide : l'effacement volontaire doit rester.
        _session_source = self.brd.GetPath()
        _session_folder = self.folder.GetPath() if _session_source else ''

    def on_source_changed(self,event):
        source = self.brd.GetPath()
        folder = self.folder.GetPath()
        # Suivre la source uniquement si le dossier est encore la suggestion ;
        # une destination choisie par l'utilisateur ne doit pas être écrasée.
        if not source or not folder or folder == self._suggested_folder:
            self.folder.SetPath(str(Path(source).parent) if source else '')
        self._suggested_folder = str(Path(source).parent) if source else ''
        self.target=None
        self.command.Clear()
        self.remember_source()
        self.update_actions()
        event.Skip()

    def on_folder_changed(self,event):
        self.remember_source()
        self.update_actions()
        event.Skip()

    def on_result_changed(self,event):
        self.copy_result.Enable(bool(self.result.GetValue()))
        event.Skip()

    def update_actions(self):
        source=Path(self.brd.GetPath())
        ready=(self.process is None and source.is_file()
               and source.suffix.lower() in ('.brd','.fbrd') and bool(self.folder.GetPath()))
        self.go.Enable(ready)
        self.return_button.Enable(ready)
        self.copy.Enable(ready and bool(self.command.GetValue()))
        self.command.Enable(ready and bool(self.command.GetValue()))
        self.open.Enable(ready and self.target is not None)
        self.copy_result.Enable(bool(self.result.GetValue()))

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
        self.diagnostic_folder=job
        self.process=subprocess.Popen([str(python),'-B','-u','-X','utf8',str(Path(__file__).with_name('worker.py')),str(config_path)],
            stdout=self.log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        for control in (self.go,self.return_button,self.brd,self.folder,self.close_button): control.Disable()
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
        for control in (self.brd,self.folder,self.close_button): control.Enable()
        try:
            result=json.loads((self.job/'result.json').read_text(encoding='utf-8'))
            self.result.SetValue(result['message'])
            if result['ok']:
                self.target=Path(result['target']);self.open.Enable()
                if result.get('open_board'):
                    open_board(pcbnew,self.target,with_krt=result.get('open_krt',False))
        except Exception as error:
            self.result.AppendText('\n'+str(error)+'\nConsulter '+str(self.job/'execution.log'))
        self.update_actions()

    def prepare(self,event,operation):
        self.remember_source()
        self.go.Disable();self.return_button.Disable();self.open.Disable();self.target=None
        self.diagnostic_folder=None
        run.last_job=None
        self.command.Clear();self.copy.Disable()
        try:
            source=Path(self.brd.GetPath())
            if not source.is_file() or source.suffix.lower() not in ('.brd','.fbrd'):
                raise ValueError('Sélectionner un fichier source Fusion .brd ou .fbrd.')
            if not self.folder.GetPath():
                raise ValueError('Choisir un dossier des résultats.')
            if operation=='to_kicad':
                self.start_worker()
                return
            if operation!='from_kicad':
                raise ValueError('Opération inconnue.')
            # Préparer un livrable uniquement ; le RUN est lancé par l'utilisateur.
            with wx.BusyCursor():
                self.board=pcbnew.GetBoard()
                self.target,message=run(pcbnew,self.board,self.brd.GetPath(),self.folder.GetPath(),False)
            self.result.SetValue(message);self.open.Enable()
            self.command.SetValue(run_command(self.target));self.copy.Enable()
        except Exception as error:
            self.diagnostic_folder=getattr(run,'last_job',None)
            message=str(error)
            if self.diagnostic_folder: message+='\nRapports : '+str(self.diagnostic_folder)
            self.result.SetValue(message)
            print(traceback.format_exc())
        finally:
            if self.process is None:
                self.update_actions()

    def copy_command(self,event):
        self.copy_text(self.command.GetValue())

    def copy_result_text(self,event):
        self.copy_text(self.result.GetValue())

    def copy_text(self,text):
        # Le presse-papiers change uniquement sur un clic explicite.
        if not text: return
        if not wx.TheClipboard.Open():
            wx.MessageBox('Presse-papiers occupé. Réessayer.', 'Copie impossible', parent=self)
            return
        try:
            copied=wx.TheClipboard.SetData(wx.TextDataObject(text))
            if copied: wx.TheClipboard.Flush()
        finally:
            wx.TheClipboard.Close()
        if not copied:
            wx.MessageBox('Le texte n’a pas pu être copié.', 'Copie impossible', parent=self)


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
