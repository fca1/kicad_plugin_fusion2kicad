"""Assembler le plugin autonome, sans serveur ni chemin vers le dépôt."""
from pathlib import Path
import shutil
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def build(destination):
    package=Path(destination)/'passerelle_fusion'
    if package.exists(): raise ValueError('Dossier de paquet déjà présent : choisir une destination neuve.')
    shutil.copytree(ROOT/'plugin/passerelle_fusion',package,ignore=shutil.ignore_patterns('__pycache__'))
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
    print(build(ap.parse_args().destination))
