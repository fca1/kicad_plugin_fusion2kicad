"""Assembler le plugin autonome, sans serveur ni chemin vers le dépôt."""
from pathlib import Path
import shutil
import zipfile
import json
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def version_metadata(version_repo=None):
    # Une archive connaît son commit grâce à export-subst. Le dépôt d'origine
    # fournit le compte de ses ancêtres, sans confondre ce commit avec son HEAD.
    archived = (ROOT/'plugin-version-archive.txt').read_text(encoding='utf-8').strip()
    is_archive = not archived.startswith('$Format:')
    repo = Path(version_repo) if version_repo else ROOT
    if not (repo/'.git').exists():
        raise ValueError('Assembler cette archive avec --version-repo <dépôt Git source>.')
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
    commit = archived if is_archive else git('rev-parse', 'HEAD')
    revision = int(git('rev-list', '--count', commit))
    # Les changements de documentation ne changent pas le code embarqué.
    dirty = not is_archive and bool(git('status', '--porcelain', '--',
        'plugin', 'tools', 'fusion_addin', '.gitattributes', 'plugin-version-archive.txt'))
    return dict(revision=revision, commit=commit, dirty=dirty)


def build(destination, version_repo=None):
    version = version_metadata(version_repo)
    package=Path(destination)/'passerelle_fusion'
    if package.exists(): raise ValueError('Dossier de paquet déjà présent : choisir une destination neuve.')
    shutil.copytree(ROOT/'plugin/passerelle_fusion',package,ignore=shutil.ignore_patterns('__pycache__'))
    (package/'_version.json').write_text(json.dumps(version, indent=2)+'\n', encoding='utf-8')
    core=package/'core';core.mkdir()
    (core/'__init__.py').write_text('',encoding='utf-8')
    for name in ('regles_fusion_kicad.py','kicad_routage_vers_fusion.py','verifier_script_routage.py','jonctions_fusion.py','cuivre_jonctions.py','fusion_source.py','polygon_replacement.py'):
        shutil.copy2(ROOT/'tools'/name,core/name)
    with zipfile.ZipFile(Path(destination)/'passerelle_fusion.zip','w',zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(package.rglob('*')):
            if path.is_file(): archive.write(path,path.relative_to(destination))
    # Le compagnon est installé séparément dans les compléments Fusion.
    shutil.copytree(ROOT/'fusion_addin/PasserelleKiCad', Path(destination)/'PasserelleKiCad',
                    ignore=shutil.ignore_patterns('__pycache__'))
    return package


if __name__=='__main__':
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('destination',type=Path)
    ap.add_argument('--version-repo', type=Path, help='Dépôt source pour versionner une archive Git')
    args = ap.parse_args()
    print(build(args.destination, args.version_repo))
