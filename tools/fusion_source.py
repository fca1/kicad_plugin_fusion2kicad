"""Lecture BRD/FBRD et traduction XML, séparée du plugin et des règles.

Le BRD produit est une entrée de l'importeur EAGLE, avec les règles modernes
conservées pour notre lecteur séparé. Ce n'est pas un export natif Autodesk.
"""
from pathlib import Path
import copy
import hashlib
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET

MAX_BYTES = 128 * 1024 * 1024


def _inset(vertices, amount):
    """Contour extérieur Fusion -> ligne médiane EAGLE (moitié de width).

    Le moteur géométrique de KiCad évite de réimplémenter le clipping de
    polygones concaves. Aucun document KiCad n'est chargé ici. Les arcs sont
    discrétisés avec une erreur de corde <= 1 µm, dans les unités XML en mm.
    """
    import pcbnew
    poly = pcbnew.SHAPE_POLY_SET(); poly.NewOutline()
    for i, vertex in enumerate(vertices):
        x,y = float(vertex.get('x')),float(vertex.get('y'))
        end = vertices[(i+1)%len(vertices)]
        ex,ey = float(end.get('x')),float(end.get('y'))
        theta = math.radians(float(vertex.get('curve','0')))
        poly.Append(round(x*1e6),round(y*1e6))
        if abs(theta) < 1e-12:
            continue
        if not 0 < abs(theta) < 2*math.pi or (x,y)==(ex,ey):
            raise ValueError('Arc de contour dégénéré.')
        dx,dy = ex-x,ey-y
        cx=(x+ex)/2-dy/(2*math.tan(theta/2))
        cy=(y+ey)/2+dx/(2*math.tan(theta/2))
        radius=math.hypot(x-cx,y-cy)
        angle=math.atan2(y-cy,x-cx)
        step=2*math.acos(max(-1,1-min(.001/radius,1)))
        count=max(1,math.ceil(abs(theta)/step))
        if count>100000:
            raise ValueError('Arc trop complexe.')
        for j in range(1,count):
            a=angle+theta*j/count
            poly.Append(round((cx+radius*math.cos(a))*1e6),round((cy+radius*math.sin(a))*1e6))
    poly.Deflate(round(amount*1e6),pcbnew.CORNER_STRATEGY_ROUND_ALL_CORNERS,1000)
    if poly.OutlineCount()!=1 or poly.HoleCount(0):
        raise ValueError('Le décalage produit un polygone vide, multiple ou troué : conversion refusée.')
    outline=poly.COutline(0)
    return [ET.Element('vertex',x=format(outline.CPoint(i).x/1e6,'.9g'),
                       y=format(outline.CPoint(i).y/1e6,'.9g')) for i in range(outline.PointCount())]


def read_source(path):
    path = Path(path)
    if path.suffix.lower() not in ('.brd', '.fbrd'):
        raise ValueError('Sélectionner une carte .brd ou .fbrd.')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Source trop volumineuse.')
    data = path.read_bytes()
    member = None
    if path.suffix.lower() == '.fbrd':
        with zipfile.ZipFile(path) as archive:
            boards = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith('.brd')]
            if len(boards) != 1 or boards[0].file_size > MAX_BYTES:
                raise ValueError('Archive FBRD : un seul BRD embarqué de taille admissible est requis.')
            member = boards[0].filename
            # Lire en mémoire : aucun chemin de l'archive n'est extrait sur disque.
            data = archive.read(boards[0])
    if b'<!ENTITY' in data.upper():
        raise ValueError('Entités XML non prises en charge.')
    root = ET.fromstring(data)
    if root.tag != 'eagle' or root.find('./drawing/board') is None:
        raise ValueError('Source sans carte XML Fusion/EAGLE.')
    return root, data, member


def _polygon(node, width):
    allowed = {'layer', 'width', 'isolate', 'thermals', 'orphans', 'rank',
               'hatchspacing', 'thermalwidth', 'fillvisible', 'pour'}
    if set(node.attrib) - allowed:
        raise ValueError('Attribut de polygone non pris en charge : '+str(set(node.attrib)-allowed))
    allowed_children = {'polygonoutlineobjects', 'polygonoutlinesegments'}
    if node.tag == 'polygonpour':
        allowed_children.add('polygonfilldetails')
    if any(x.tag not in allowed_children for x in node):
        raise ValueError('Découpe ou objet de polygone non pris en charge.')
    # Les contours segmentés fournis par Fusion sont la référence pour les
    # formes graphiques. Les pours gardent leurs arcs mathématiques ; leur
    # remplissage mis en cache n'est pas une découpe utilisateur et sera recalculé.
    contour = node.find('polygonoutlinesegments')
    if contour is None:
        contour = node.find('polygonoutlineobjects')
    if contour is None or any(x.tag != 'vertex' for x in contour):
        raise ValueError('Contour de polygone non constitué de vertices.')
    vertices = [copy.deepcopy(x) for x in contour]
    if len(vertices) > 1 and vertices[0].attrib == vertices[-1].attrib:
        vertices.pop()
    if len(vertices) < 3:
        raise ValueError('Contour de polygone dégénéré.')
    attrs = {k:v for k,v in node.attrib.items() if k in {'layer','width','isolate','thermals','orphans','rank','pour'}}
    attrs.setdefault('width', width)
    attrs.setdefault('pour', 'solid')
    if 'hatchspacing' in node.attrib:
        attrs['spacing'] = node.get('hatchspacing')
    if node.tag == 'polygonpour':
        attrs.setdefault('thermals', 'yes')
    poly = ET.Element('polygon', attrs)
    poly.extend(_inset(vertices,float(attrs['width'])/2))
    return poly


def translate(root):
    """Retourner un nouvel arbre et un rapport, sans modifier l'arbre source."""
    root = copy.deepcopy(root)
    board = root.find('./drawing/board')
    layers = root.find('./drawing/layers')
    if layers is None:
        raise ValueError('Définition des couches absente.')
    setup = board.find('./designrules/param[@name="layerSetup"]')
    if setup is None:
        raise ValueError('Empilement layerSetup absent.')
    raw = setup.get('value', '')
    if not re.fullmatch(r'[()\d+*]+', raw):
        raise ValueError('Empilement non pris en charge.')
    copper = list(dict.fromkeys(int(s) for s in re.findall(r'\d+', raw)))
    # Trois empilements démontrés : 2 couches anciennes/étendues et 4 couches
    # étendues. Aucun rabattement arbitraire des couches borgnes ou enterrées.
    supported = {(1,16): {1:1,16:16}, (1,64): {1:1,304:16},
                 (1,2,63,64): {1:1,2:2,303:15,304:16},
                 (1,2,15,16): {1:1,2:2,15:15,16:16}}
    mapping = supported.get(tuple(copper))
    if mapping is None:
        raise ValueError('Empilement non pris en charge par le traducteur : '+raw)
    ordinal = {1:1,64:16,63:15,2:2} if copper[-1] == 64 else {x:x for x in copper}
    setup.set('value', re.sub(r'\d+', lambda m:str(ordinal[int(m[0])]), raw))
    warnings = []
    count = {'polygons':0, 'cached_fills':0, 'instance_pads':0}
    # La définition d'une couche non utilisée est omise ; une primitive sur une
    # couche cuivre non déclarée doit en revanche bloquer l'import.
    def copper_layer(n):
        return 1 <= n <= 16 or 257 <= n <= 304
    for layer in list(layers):
        number = int(layer.get('number'))
        if copper_layer(number) and number not in mapping:
            layers.remove(layer)
            continue
        layer.set('number', str(mapping.get(number, number)))
        layer.set('color', layer.get('color', '7'))
        for key in list(layer.attrib):
            if key not in {'number','name','color','fill','visible','active'}:
                del layer.attrib[key]
    for via in board.findall('./signals/signal/via'):
        expected = f'{copper[0]}-{copper[-1]}'
        if via.get('extent') != expected:
            raise ValueError('Via non traversant non pris en charge : '+str(via.get('extent')))
        via.set('extent', '1-16')
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag in ('polygonshape','polygonpour') and parent.tag != 'polygonfilldetails':
                # Une fois un pour remplacé, ses caches restent dans l'ancien
                # sous-arbre ; ils ne sont jamais insérés dans le résultat.
                width = '0.1524'
                count['polygons'] += 1
                count['cached_fills'] += len(child.findall('polygonfilldetails'))
                index = list(parent).index(child)
                parent.remove(child); parent.insert(index, _polygon(child, width))
            elif child.tag == 'pad' and parent.tag == 'element':
                if set(child.attrib) != {'name'} or len(child):
                    raise ValueError('Surcharge de pastille par instance non prise en charge.')
                parent.remove(child); count['instance_pads'] += 1
            elif child.tag in ('layerstackup', 'sketches', 'constructions', 'namedviews'):
                parent.remove(child)
                warnings.append('Métadonnée '+child.tag+' conservée dans la source, non transférée ; empilement physique à contrôler.')
    allowed_tags = set('eagle drawing settings setting drillsymbolsettings drill grid layers layer board description plain libraries library packages package description wire circle rectangle hole text dimension frame polygon vertex pad smd packages3d package3d packageinstances packageinstance attributes attribute variantdefs variantdef classes class clearance designrules param rules rule rulecategory autorouter pass elements element signals signal contactref via errors approved compatibility note mfgpreviewcolors mfgpreviewcolor fusionsync fusionteam groups board_group filters filter'.split())
    for node in root.iter():
        if node.tag not in allowed_tags:
            raise ValueError('Objet XML non pris en charge : '+node.tag)
        if 'layer' in node.attrib:
            number = int(node.get('layer'))
            if copper_layer(number) and number not in mapping:
                raise ValueError('Objet sur couche cuivre hors empilement : '+str(number))
            node.set('layer', str(mapping.get(number, number)))
        if 'solderPaste' in node.attrib:
            if node.get('solderPaste') not in ('off','on','auto'):
                raise ValueError('Mode de pâte inconnu.')
            node.set('cream', 'no' if node.attrib.pop('solderPaste', None) == 'off' else 'yes')
        if node.tag in ('smd','pad') and 'solderMask' in node.attrib:
            if node.get('solderMask') == 'offset':
                # EAGLE ne porte pas ce décalage par pastille. L'export Autodesk
                # l'omet aussi ; conserver la perte explicitement dans le rapport.
                warnings.append('Décalage local de masque de pastille non transféré : '+str(node.get('solderMaskOffset'))+' mm ; vérifier le masque dans KiCad.')
                node.attrib.pop('solderMaskOffset', None)
                node.set('solderMask', 'on')
            if node.get('solderMask') not in ('off','on','auto'):
                raise ValueError('Mode de masque de pastille inconnu.')
            node.set('stop','no' if node.attrib.pop('solderMask')=='off' else 'yes')
        if node.tag == 'via' and 'solderMask' in node.attrib:
            if node.get('solderMask') != 'auto':
                raise ValueError('Masque de via non pris en charge.')
            del node.attrib['solderMask']; node.set('alwaysstop','yes')
    for setting in root.findall('./drawing/settings/setting'):
        setting.attrib.pop('extendedlayers', None)
    warnings += ['Couleurs d’affichage Fusion remplacées par une couleur EAGLE neutre lorsqu’absentes.',
                 'Remplissages cuivre recalculables ; caches de remplissage Fusion non importés.',
                 'Contours polygonaux recalculés : les détails fins des graphismes complexes peuvent différer de l’export Autodesk ; contrôler notamment les logos.']
    return root, dict(layer_mapping=mapping, counts=count, warnings=sorted(set(warnings)),
                      modern_rules=len(board.findall('./designrules/rules/rule')))


def prepare_source(source, folder):
    """Copier l'original, produire original.brd et un manifeste dans un dossier neuf."""
    source, folder = Path(source), Path(folder)
    root, data, member = read_source(source)
    # Un BRD EAGLE déjà compatible garde ses octets et son ancien comportement.
    native = member is not None or any('blackcolor' in x.attrib for x in root.findall('./drawing/layers/layer'))
    report = {'source':str(source.resolve()), 'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
              'archive_member':member, 'translated':native}
    if native:
        root, details = translate(root); report.update(details)
        output = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    else:
        output = data
        report.update(modern_rules=len(root.findall('./drawing/board/designrules/rules/rule')), warnings=[])
    folder.mkdir(parents=True, exist_ok=True)
    if member:
        with (folder/'source.fbrd').open('xb') as stream: stream.write(source.read_bytes())
    with (folder/'source_interne.brd').open('xb') as stream: stream.write(data)
    target = folder/'original.brd'
    with target.open('xb') as stream: stream.write(output)
    report['output_sha256'] = hashlib.sha256(output).hexdigest()
    (folder/'rapport_source.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return target, report


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    target, _ = prepare_source(args.source, args.destination)
    print(target)

