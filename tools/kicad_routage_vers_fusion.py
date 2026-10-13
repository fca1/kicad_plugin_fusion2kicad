"""Convertit les pistes/vias de cette carte KiCad en script Fusion, sans toucher aux sources.

La correspondance est établie par références ET pastilles ; aucun miroir de PCB0/PCB1
n'est ajouté. Seul le changement de repère de l'import EAGLE est inversé.
Les objets non pris en charge provoquent un arrêt plutôt qu'une conversion partielle.
"""
import argparse
import collections
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
import xml.etree.ElementTree as ET


def parse(text):
    tokens = re.findall(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text)
    stack, root = [], None
    for t in tokens:
        if t == '(':
            node = []
            if stack:
                stack[-1].append(node)
            else:
                assert root is None, 'Plusieurs racines'
                root = node
            stack.append(node)
        elif t == ')':
            stack.pop()
        else:
            # Les fichiers fournis emploient les échappements usuels de KiCad.
            value = re.sub(r'\\(["\\])', r'\1', t[1:-1]) if t.startswith('"') else t
            stack[-1].append(value)
    assert not stack and root[0] == 'kicad_pcb'
    return root


def nodes(n, key):
    return [x for x in n if isinstance(x, list) and x and x[0] == key]


def field(n, key, default=None):
    found = nodes(n, key)
    return found[0][1:] if found else default


def net(n):
    value = field(n, 'net', [''])
    assert len(value) == 1, 'Seul le format KiCad 10 à nets nommés est pris en charge'
    return value[0]


def fmt(n):
    return f'{n:.6f}'.rstrip('0').rstrip('.') if abs(n) > 0.0000005 else '0'


def quoted(s):
    assert s and not any(c in s for c in "'\\\r\n;*?[]"), f'Nom non sûr pour le script : {s!r}'
    return "'" + s + "'"


def point(v):
    return '(' + ' '.join(fmt(x) for x in v) + ')'


def sig(w):
    # L'ordre des deux extrémités n'a pas de sens électrique.
    return (w['net'], w['layer'], round(w['width'], 6),
            tuple(sorted(tuple(round(v, 6) for v in w[k]) for k in ('start', 'end'))))


def copper_zones(k, layer_map, net_map, transform):
    """Conserve des contours simples, au plus un par couche cuivre.

    Les priorités concurrentes, trous et thermiques n'ont pas une traduction
    démontrée. Les refuser évite un résultat apparemment complet mais différent.
    """
    zones = nodes(k, 'zone')
    occupied_layers = set()
    result = []
    for z in zones:
        allowed = {'net', 'layer', 'uuid', 'name', 'hatch', 'connect_pads',
                   'min_thickness', 'fill', 'polygon', 'filled_polygon', 'priority', 'locked'}
        assert all(isinstance(x, list) and x[0] in allowed for x in z[1:]), 'Zone : option non prise en charge (keepout, couches ou réglage spécifique)'
        assert field(z, 'layer') in ([l] for l in layer_map), 'Zone : couche cuivre unique requise'
        layer = field(z, 'layer')[0]
        assert layer not in occupied_layers, 'Zones multiples sur une même couche : interactions de remplissage non prises en charge'
        occupied_layers.add(layer)
        priority = field(z, 'priority', ['0'])
        assert len(priority) == 1 and re.fullmatch(r'[0-9]+', priority[0]), 'Zone : priorité invalide'
        # Sans autre zone sur cette couche, la priorité ne départage aucun
        # remplissage. Le rang Fusion 1 est donc conservé indépendamment de
        # la priorité KiCad. Les polygones déjà présents en cible restent
        # refusés par l'ULP avant application.
        cp = field(z, 'connect_pads', [])
        assert cp and cp[0] == 'yes', 'Zone : seules les connexions directes sont prises en charge'
        assert len(cp) == 2 and cp[1][0] == 'clearance', 'Zone : isolation explicite requise'
        clearance = float(cp[1][1])
        width = float(field(z, 'min_thickness')[0])
        assert math.isfinite(clearance) and clearance >= 0 and math.isfinite(width) and width > 0, 'Zone : dimensions invalides'
        fill = field(z, 'fill', [])
        assert fill and fill[0] == 'yes', 'Zone : remplir dans KiCad avant conversion'
        assert all(isinstance(x, list) and x[0] in {'thermal_gap', 'thermal_bridge_width', 'island_removal_mode', 'mode'} for x in fill[1:]), 'Zone : paramètre de remplissage non pris en charge'
        assert field(['fill']+fill, 'mode', ['polygon']) == ['polygon'], 'Zone hachurée non prise en charge'
        island_mode = field(['fill']+fill, 'island_removal_mode', ['2'])
        assert island_mode in (['0'], ['1']), 'Zone : suppression des îlots selon leur aire non prise en charge'
        polygons = nodes(z, 'polygon')
        assert len(polygons) == 1 and len(polygons[0]) == 2, 'Zone : contour unique sans trou requis'
        pts = field(polygons[0], 'pts', [])
        assert 3 <= len(pts) <= 1000 and all(p[0] == 'xy' and len(p) == 3 for p in pts), 'Zone : contour droit requis'
        xy = [transform(p[1:]) for p in pts]
        assert all(math.isfinite(v) for p in xy for v in p), 'Zone : coordonnées invalides'
        assert len(set(map(tuple, xy))) == len(xy), 'Zone : sommets répétés ou trou non pris en charge'
        # Intersections et contacts entre arêtes non adjacentes sont ambigus pour
        # les deux moteurs. Même les intersections colinéaires sont rejetées.
        def cross(a, b, c):
            return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        for i, a in enumerate(xy):
            b = xy[(i+1) % len(xy)]
            for j in range(i+2, len(xy)):
                if i == 0 and j == len(xy)-1: continue
                c, d = xy[j], xy[(j+1) % len(xy)]
                overlap = all(max(min(a[t], b[t]), min(c[t], d[t])) <= min(max(a[t], b[t]), max(c[t], d[t])) for t in (0, 1))
                assert not (overlap and cross(a,b,c)*cross(a,b,d) <= 0 and cross(c,d,a)*cross(c,d,b) <= 0), 'Zone : contour auto-intersectant'
        assert abs(sum(cross([0,0], a, xy[(i+1)%len(xy)]) for i,a in enumerate(xy))) > 1e-10, 'Zone : aire nulle'
        for fp in nodes(k, 'footprint'):
            for item in [fp]+nodes(fp, 'pad'):
                assert field(item, 'zone_connect', ['2']) == ['2'], 'Zone : connexion locale de pastille non directe'
        signal = net_map.get(net(z))
        assert signal, 'Zone : signal non associé aux contacts Fusion'
        result.append(dict(net=signal, layer=layer_map[field(z,'layer')[0]], width=width,
                           isolate=clearance, vertices=xy, thermals=False, orphans=island_mode == ['1'], rank=1))
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('kicad', type=Path)
    ap.add_argument('original', type=Path)
    ap.add_argument('--output', type=Path)
    ap.add_argument('--name', default='cube_pcb1', help='Nom du livrable (lettres, chiffres, _ et -)')
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--prepare-only', action='store_true', help="L'ULP génère le SCR sans l'exécuter")
    ap.add_argument('--quiet-prepare', action='store_true', help='Sans dialogue de succès ; exige --prepare-only pour le compagnon Fusion')
    mode.add_argument('--confirm-apply', action='store_true', help="L'ULP propose d'appliquer le SCR après les contrôles (2 couches)")
    ap.add_argument('--strict-via-options', action='store_true', help='Refuser les options de fabrication de vias non transférables')
    ap.add_argument('--no-preserve-via-mask', action='store_true', help='Ne pas conserver les ouvertures forcées du masque original')
    args = ap.parse_args(argv)
    if args.quiet_prepare and not args.prepare_only:
        ap.error('--quiet-prepare exige --prepare-only.')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', args.name):
        ap.error('Nom invalide : 1 à 64 lettres/chiffres, tirets ou underscores.')
    if __debug__ is False:
        ap.error('Ne pas utiliser Python -O : les contrôles doivent rester actifs.')
    k = parse(args.kicad.read_text(encoding='utf-8-sig'))
    document = ET.parse(args.original).getroot()
    b = document.find('./drawing/board')
    assert b is not None
    assert not nodes(k, 'arc'), 'Arcs de pistes KiCad : conversion non prise en charge'
    try:
        from .regles_fusion_kicad import copper_mapping, mm
        from .jonctions_fusion import normalize
        from .cuivre_jonctions import ulp_pad_guard
        from .polygon_replacement import inventory, guard
    except ImportError:
        from regles_fusion_kicad import copper_mapping, mm
        from jonctions_fusion import normalize
        from cuivre_jonctions import ulp_pad_guard
        from polygon_replacement import inventory, guard
    layer_map = copper_mapping(b, k)
    layer_names = {int(l.get('number')): l.get('name') for l in document.findall('./drawing/layers/layer')}
    assert all(n in layer_names for n in layer_map.values()), 'Couche de l’empilement absente de la table BRD'
    inner_layers = list(layer_map.values())[1:-1]
    assert len({layer_names[n] for n in layer_map.values()}) == len(layer_map), 'Noms de couches ambigus'
    if inner_layers and not (args.prepare_only or args.confirm_apply):
        raise ValueError('Retour 4/6 couches : génération en mode préparation uniquement jusqu’à validation des couches dans Fusion.')
    elements = {e.get('name'): e for e in b.findall('./elements/element')}
    packages = {(lib.get('name'), p.get('name')): p for lib in b.findall('./libraries/library') for p in lib.findall('./packages/package')}
    signals = {s.get('name'): s for s in b.findall('./signals/signal')}
    contacts = {(c.get('element'), c.get('pad')): name for name, s in signals.items() for c in s.findall('contactref')}
    footprints, ignored, hole_fps = {}, [], []
    for fp in nodes(k, 'footprint'):
        ref = next((p[2] for p in nodes(fp, 'property') if p[1] == 'Reference'), '')
        if ref in elements:
            assert ref not in footprints
            footprints[ref] = fp
        else:
            assert all(p[2] == 'np_thru_hole' and not net(p) for p in nodes(fp, 'pad')), f'Composant supplémentaire avec pads : {ref}'
            hole_fps.append(fp)
            ignored.append(ref)
    assert set(footprints) == set(elements), f'Composants manquants : {set(elements)-set(footprints)}'
    dx = statistics.median(float(field(footprints[r], 'at')[0])-float(e.get('x')) for r, e in elements.items())
    sy = statistics.median(float(field(footprints[r], 'at')[1])+float(e.get('y')) for r, e in elements.items())

    def transform(xy):
        return [round(float(xy[0])-dx, 6), round(sy-float(xy[1]), 6)]

    # EAGLE stocke les trous libres dans plain ; KiCad les importe comme empreintes.
    holes_original = sorted((float(h.get('x')), float(h.get('y')), float(h.get('drill'))) for h in b.findall('./plain/hole'))
    holes_kicad = []
    for fp in hole_fps:
        for p in nodes(fp, 'pad'):
            assert field(p, 'at')[:2] == ['0', '0']
            holes_kicad.append((*transform(field(fp, 'at')), float(field(p, 'drill')[0])))
    assert sorted(holes_kicad) == holes_original, 'Trous mécaniques modifiés'

    errors, pad_report, net_map, reverse_net_map = [], [], {}, {}
    anchors = []
    design_rules = {p.get('name'):p.get('value') for p in b.findall('./designrules/param')}
    for ref, e in elements.items():
        fp = footprints[ref]
        at = list(map(float, field(fp, 'at')))
        ex, ey = float(e.get('x')), float(e.get('y'))
        distance = math.dist(transform(at), [ex, ey])
        if distance > 0.000002:
            errors.append(f'{ref}: origine déplacée de {distance} mm')
        rot = e.get('rot', 'R0')
        assert field(fp, 'layer') == (['B.Cu'] if 'M' in rot else ['F.Cu'])
        er = math.radians(float(rot.replace('S', '').replace('M', '').replace('R', '')))
        kr = math.radians(at[2] if len(at) > 2 else 0)
        package = packages[(e.get('library'), e.get('package'))]
        epads = {p.get('name'): p for p in package if p.tag in ('pad', 'smd')}
        kpads = {p[1]: p for p in nodes(fp, 'pad') if p[2] != 'np_thru_hole'}
        assert len(kpads) == len([p for p in nodes(fp, 'pad') if p[2] != 'np_thru_hole']), f'Pads dupliqués : {ref}'
        assert set(kpads) == set(epads), f'Pastilles différentes : {ref}'
        for number, ep in epads.items():
            kp = kpads[number]
            px, py = float(ep.get('x')), float(ep.get('y'))
            if 'M' in rot:
                px = -px
            eagle_xy = [ex+px*math.cos(er)-py*math.sin(er), ey+px*math.sin(er)+py*math.cos(er)]
            px, py = map(float, field(kp, 'at')[:2])
            kxy = [at[0]+px*math.cos(kr)+py*math.sin(kr), at[1]-px*math.sin(kr)+py*math.cos(kr)]
            err = math.dist(eagle_xy, transform(kxy))
            kn, en = net(kp), contacts.get((ref, number), '')
            # La notation des signaux actifs bas diffère (!CMD_RESET / ~{CMD_RESET}).
            # Seule une bijection prouvée par TOUS les contacts autorise la conversion.
            if kn in net_map and net_map[kn] != en or en in reverse_net_map and reverse_net_map[en] != kn or bool(kn) != bool(en):
                errors.append(f'{ref}.{number}: connectivité incohérente {en!r} -> {kn!r}')
            net_map[kn], reverse_net_map[en] = en, kn
            if err > 0.000002:
                errors.append(f'{ref}.{number}: erreur {err:.9f} mm, net {en!r} -> {kn!r}')
            pad_report.append({'component': ref, 'pad': number, 'net': en, 'xy_fusion': eagle_xy, 'error_mm': err})
            # Disque inscrit commun aux deux pastilles : limite conservatrice,
            # indépendante de leur rotation. Les diamètres automatiques sont
            # calculés avec les anneaux ; les formes personnalisées sont exclues.
            size = list(map(float, field(kp, 'size')))
            radius = 0.
            if kp[3] in ('circle', 'rect', 'roundrect', 'oval'):
                if ep.tag == 'smd':
                    radius = min(float(ep.get('dx')), float(ep.get('dy')), *size)/2
                elif ep.get('shape', 'round') in ('round', 'square', 'octagon'):
                    diameter = float(ep.get('diameter', '0'))
                    if diameter == 0:
                        # Diamètre automatique EAGLE : perçage et anneaux des
                        # trois catégories de couches. Retenir le plus petit.
                        drill = float(ep.get('drill'))
                        rings = [min(mm(design_rules['rlMaxPad'+side]),
                                     max(mm(design_rules['rlMinPad'+side]),
                                         drill*float(design_rules['rvPad'+side])))
                                 for side in ('Top','Inner','Bottom')]
                        diameter = drill+2*min(rings)
                    radius = min(diameter, *size)/2
                    if ep.get('shape') == 'octagon': radius *= math.cos(math.pi/8)
            if radius and en:
                layers = list(layer_map.values()) if ep.tag == 'pad' else [
                    list(layer_map.values())[-1] if ('M' in rot) != (ep.get('layer') == '16') else 1]
                # Formes cuivre réelles, orientées dans le repère Fusion.
                # Leur intersection sert à prouver que les raccords n'ajoutent
                # de cuivre ni à la source KiCad ni à la géométrie cible.
                angle_k = float(field(kp, 'at')[2]) if len(field(kp, 'at')) > 2 else 0.
                source_shape = dict(kind='rect', size=size, at=eagle_xy,
                                    angle=angle_k)
                if kp[3] == 'circle':
                    source_shape = dict(kind='circle', diameter=min(size), at=eagle_xy)
                elif kp[3] == 'oval':
                    source_shape['radius'] = min(size)/2
                elif kp[3] == 'roundrect':
                    source_shape['radius'] = min(size)*float(field(kp,'roundrect_rratio',['0'])[0])
                copper = {}
                for layer in layers:
                    if ep.tag == 'pad':
                        side = 'Top' if layer == 1 else 'Bottom' if layer == list(layer_map.values())[-1] else 'Inner'
                        drill = float(ep.get('drill'))
                        effective = max(float(ep.get('diameter','0')), drill+2*min(
                            mm(design_rules['rlMaxPad'+side]), max(mm(design_rules['rlMinPad'+side]),
                            drill*float(design_rules['rvPad'+side]))))
                        kind = ep.get('shape','round')
                        target_shape = dict(kind='circle' if kind=='round' else 'rect' if kind=='square' else 'octagon',
                                            diameter=effective, size=[effective,effective], at=eagle_xy)
                    else:
                        target_shape = dict(kind='rect',size=[float(ep.get('dx')),float(ep.get('dy'))],
                                            radius=min(float(ep.get('dx')),float(ep.get('dy')))*float(ep.get('roundness','0'))/200,
                                            at=eagle_xy)
                    pad_angle = float(ep.get('rot','R0').replace('R',''))
                    target_shape['angle'] = math.degrees(er) + (-pad_angle if 'M' in rot else pad_angle)
                    copper[str(layer)] = [source_shape,target_shape]
                anchors.append(dict(net=en, at=eagle_xy, radius=radius, layers=layers,
                                    copper=copper, source=f'{ref}.{number}'))
                pad_report[-1]['junction_copper'] = copper
    assert not errors, '\n'.join(errors)
    zones = copper_zones(k, layer_map, net_map, transform)
    wires = []
    for s in nodes(k, 'segment'):
        sn = net_map[net(s)]
        assert sn in signals
        w = {'net': sn, 'layer': layer_map[field(s, 'layer')[0]], 'width': float(field(s, 'width')[0]),
             'start': transform(field(s, 'start')), 'end': transform(field(s, 'end'))}
        assert w['width'] > 0 and w['start'] != w['end']
        wires.append(w)
    vias = []
    for v in nodes(k, 'via'):
        assert v[1] not in ('blind', 'micro')
        assert field(v, 'layers') == ['F.Cu', 'B.Cu']
        assert net_map[net(v)] in signals
        item = {'net': net_map[net(v)], 'at': transform(field(v, 'at')), 'diameter': float(field(v, 'size')[0]), 'drill': float(field(v, 'drill')[0])}
        item['capping'] = field(v, 'capping', ['no'])[0]
        item['filling'] = field(v, 'filling', ['no'])[0]
        assert item['diameter'] > item['drill'] > 0
        vias.append(item)
    anchors.extend(dict(net=v['net'], at=v['at'], radius=v['diameter']/2,
                        layers=list(layer_map.values()), source='via') for v in vias)
    class_widths = {c.get('number'):float(c.get('width','0')) for c in b.findall('./classes/class')}
    for anchor in anchors:
        anchor['min_width'] = max(mm(design_rules['msWidth']),class_widths[signals[anchor['net']].get('class','0')])
    unresolved = []
    fusion_wires, junctions = normalize(wires, anchors, unresolved)
    old_wires = []
    for name, s in signals.items():
        for w in s.findall('wire'):
            if int(w.get('layer')) not in layer_map.values():
                continue
            assert float(w.get('curve', '0')) == 0, 'Arc original non pris en charge'
            old_wires.append({'net': name, 'layer': int(w.get('layer')), 'width': float(w.get('width')),
                              'start': [float(w.get('x1')), float(w.get('y1'))], 'end': [float(w.get('x2')), float(w.get('y2'))]})
    old_vias = [{'net': name, **v.attrib} for name,s in signals.items() for v in s.findall('via')]
    assert all(v.get('extent') == f'{list(layer_map.values())[0]}-{list(layer_map.values())[-1]}' for v in old_vias), 'Via original non traversant'
    existing = collections.Counter(map(sig, old_wires))
    desired = collections.Counter(map(sig, wires))
    report = {'source_kicad': str(args.kicad.resolve()), 'source_brd': str(args.original.resolve()),
              'sha256_kicad': hashlib.sha256(args.kicad.read_bytes()).hexdigest(),
              'sha256_brd': hashlib.sha256(args.original.read_bytes()).hexdigest(),
              'transform': {'x_fusion': f'x_kicad - {fmt(dx)}', 'y_fusion': f'{fmt(sy)} - y_kicad'},
              'components_checked': len(footprints), 'pads_checked': len(pad_report), 'ignored_graphic_footprints': ignored,
              'max_pad_error_mm': max(p['error_mm'] for p in pad_report),
              'net_name_mapping': {a:v for a,v in net_map.items() if a != v},
              'segments_kicad': len(wires), 'vias_kicad': len(vias), 'nets_routed': len(set(w['net'] for w in wires)),
              'original_copper_segments': len(old_wires), 'original_vias': old_vias,
              'original_segments_missing_exactly_in_kicad': sum((existing-desired).values()),
              'widths_mm': sorted(set(w['width'] for w in wires)),
              'via_sizes_mm': sorted(set((v['diameter'],v['drill']) for v in vias)),
              'via_fabrication_flags_not_transferred': [v for v in vias if v['capping'] != 'no' or v['filling'] != 'no'],
              'pads': pad_report, 'segments': wires, 'vias': vias, 'zones': zones,
              'zones_kicad': len(zones),
              'zone_fill_recomputed_by_fusion': bool(zones),
              'fusion_segments': fusion_wires, 'junctions': junctions,
              'unresolved_junctions': unresolved,
              'layer_mapping':layer_map, 'source_layer_names':layer_names,
              'fusion_layer_mapping_verified':False}
    report['ulp_filename'] = args.name + '_pistes_vias.ulp'
    report['options'] = {'name': args.name, 'prepare_only': args.prepare_only or bool(inner_layers),
                         'confirm_apply': args.confirm_apply and not bool(inner_layers),
                         'strict_via_options': args.strict_via_options,
                         'preserve_via_mask': not args.no_preserve_via_mask}
    # Une limite de connectivité est signalée, sans empêcher de livrer le
    # cuivre source. Les raccords non prouvés ne sont jamais inventés.
    report['warnings'] = []
    if unresolved:
        report['warnings'].append(
            f'{len(unresolved)} contact(s) cuivre sans jonction explicite sûre ; '
            'contacts conservés sans raccord ajouté. Vérifier les chevelus dans Fusion. '
            'Voir diagnostic_jonctions.json.')
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            (args.output/'diagnostic_jonctions.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if args.strict_via_options and report['via_fabrication_flags_not_transferred']:
        raise ValueError('Options de vias non transférables : capping/filling. Choisir le mode avec avertissement ou corriger la source KiCad.')
    print(json.dumps({a:v for a,v in report.items() if a not in ('pads','segments','vias','fusion_segments','junctions')}, ensure_ascii=False, indent=2))
    if args.output:
        # L'import remplace le routage des nets concernés, pour éviter doublons et
        # anciennes branches conservées lorsque KiCad a découpé ou rerouté une piste.
        involved = sorted({w['net'] for w in wires} | {v['net'] for v in vias} | {w['net'] for w in old_wires} | {v['net'] for v in old_vias} | {z['net'] for z in zones})
        replacements = inventory(b, zones, layer_names)
        report['replaced_polygons'] = replacements
        lines = [f'# PCB {args.name} : pistes et vias issus de KiCad ; executer dans le BRD uniquement.',
                 '# Remplace le routage des nets cites. Aucun composant, pad ou net logique modifie.',
                 '# Sources et controles : voir rapport_conversion.json et README.md.',
                 'GRID MM;', 'DISPLAY @TOP@ @BOTTOM@ @PADS@ @VIAS@;', 'SET WIRE_BEND 2;', 'CHANGE STYLE Continuous;']
        if replacements:
            # Le groupe ne contient que les polygones validés par l'ULP.
            lines.append('DELETE (>0 0);')
        for name in involved:
            lines.append('RIPUP ' + quoted(name) + ';')
        for klayer, layer in layer_map.items():
            # Le nom source des couches internes évite de déduire un numéro CLI
            # Fusion du numéro XML. Leur existence est vérifiée par l'ULP.
            command = 'LAYER @TOP@;' if klayer == 'F.Cu' else 'LAYER @BOTTOM@;' if klayer == 'B.Cu' else 'LAYER '+quoted(layer_names[layer])+';'
            lines.append(command)
            for w in fusion_wires:
                if w['layer'] == layer:
                    lines.append(f"LINE {quoted(w['net'])} {fmt(w['width'])} {point(w['start'])} {point(w['end'])};")
        for v in vias:
            # L'ancien via CHARGE+ était explicitement ouvert dans le masque Fusion.
            # Conserver cette propriété, que l'import KiCad n'a pas conservée.
            stop = not args.no_preserve_via_mask and any(old['net'] == v['net'] and old.get('alwaysstop') == 'yes'
                       and math.dist([float(old['x']), float(old['y'])], v['at']) < 0.000002 for old in old_vias)
            # Ne pas laisser le drapeau STOP du via précédent se propager.
            lines.append('CHANGE STOP OFF;')
            lines.append(f"CHANGE DRILL {fmt(v['drill'])};")
            lines.append(f"VIA {quoted(v['net'])} {fmt(v['diameter'])} ROUND 1-@VIA_BOTTOM@{' STOP' if stop else ''} {point(v['at'])};")
        for z in zones:
            klayer = next(k for k,v in layer_map.items() if v == z['layer'])
            lines.append('LAYER @TOP@;' if klayer == 'F.Cu' else 'LAYER @BOTTOM@;' if klayer == 'B.Cu' else 'LAYER '+quoted(layer_names[z['layer']])+';')
            lines.extend(['CHANGE POUR SOLID;', 'CHANGE THERMALS OFF;', 'CHANGE ORPHANS '+('ON;' if z['orphans'] else 'OFF;'),
                          'CHANGE RANK 1;', f"CHANGE ISOLATE {fmt(z['isolate'])};"])
            # Retour au premier sommet : fermeture explicite du polygone.
            lines.append(f"POLYGON {quoted(z['net'])} {fmt(z['width'])} "+' '.join(point(p) for p in z['vertices']+[z['vertices'][0]])+';')
        if zones:
            lines.append('SET POLYGON_RATSNEST ON;')
        lines.append('RATSNEST;')
        args.output.mkdir(parents=True, exist_ok=True)
        # Les numéros EAGLE exportés ne sont pas ceux de toutes les versions Fusion.
        # L'ULP valide la cible, puis écrit le SCR avec les constantes de son moteur.
        ulp = [f'// Import du routage {args.name} : {len(wires)} segments, {len(vias)} vias.',
               '// Sources et limites de validation dans README.md et rapport_conversion.json.',
               '// Ne modifie ni composants, ni pads, ni connectivite logique.',
               'string refs[], packages[], padkeys[], padnets[];',
               'real ex[], ey[], angles[], px[], py[];', 'int mirrors[];',
               'string issue = "";', 'int ec = 0, pc = 0;',
               'string top = "1", bottom = "16", padsLayer = "17", viasLayer = "18";',
               'int viaBottom = 16, bottomFound = 0;',
               'string scriptPath = filesetext(argv[0], ".scr");']
        q = lambda value: json.dumps(value, ensure_ascii=True)
        for i, (ref, e) in enumerate(elements.items()):
            rot = e.get('rot', 'R0')
            angle = float(rot.replace('S','').replace('M','').replace('R',''))
            ulp.append(f'refs[{i}]={q(ref)}; packages[{i}]={q(e.get("package"))}; ex[{i}]={e.get("x")}; ey[{i}]={e.get("y")}; angles[{i}]={fmt(angle)}; mirrors[{i}]={int("M" in rot)};')
        for i, pad in enumerate(pad_report):
            ulp.append(f'padkeys[{i}]={q(pad["component"]+"/"+pad["pad"])}; padnets[{i}]={q(pad["net"])}; px[{i}]={fmt(pad["xy_fusion"][0])}; py[{i}]={fmt(pad["xy_fusion"][1])};')
        ulp.extend([
            'if (!board) { dlgMessageBox("Ouvrir le PCB cible (BRD), pas le schema."); exit(1); }',
            '// Fusion V2 : Bottom=304 dans UL_LAYER, cb en CLI, 64 dans VIA.',
            '// LAYER_BOTTOM reste 16 : ne pas utiliser cette constante pour la cible.',
            'board(B) { B.layers(L) {',
            '  if (L.name == "Bottom") {',
            '    bottomFound++;',
            '    if (L.number == 304) { top="ct"; bottom="cb"; padsLayer="cp"; viasLayer="cv"; viaBottom=64; }',
            '    else if (L.number != 16) issue="Numerotation Bottom non prise en charge.";',
            '  }',
            '} }',
            'if (bottomFound != 1) issue="Couche Bottom absente ou ambigue.";',
            '// Les controles sont effectues AVANT toute commande de modification.',
            'board(B) {',
            *(['  B.signals(S) { S.polyPours(P) { issue="Polygone deja present : source sans polygone."; } }'] if not replacements else []),
            '  B.elements(E) {',
            '    int found = -1;',
            f'    for (int i=0; i<{len(elements)}; i++) if (E.name==refs[i]) found=i;',
            '    ec++;',
            '    if (found<0) issue="Composant inattendu : " + E.name;',
            '    else {',
            '      if (abs(u2mm(E.x)-ex[found])>0.00001 || abs(u2mm(E.y)-ey[found])>0.00001 || abs(E.angle-angles[found])>0.0001 || E.mirror!=mirrors[found] || E.footprint.name!=packages[found]) issue="Implantation differente : " + E.name;',
            '    }',
            '    E.footprint.contacts(C) {',
            '      int match = -1;',
            '      string key=E.name + "/" + C.name;',
            f'      for (int j=0; j<{len(pad_report)}; j++) if (key==padkeys[j]) match=j;',
            '      pc++;',
            '      if (match<0) issue="Pad inattendu : " + key;',
            '      else if (C.signal!=padnets[match] || abs(u2mm(C.x)-px[match])>0.00001 || abs(u2mm(C.y)-py[match])>0.00001) issue="Pad ou net different : " + key;',
            *ulp_pad_guard(pad_report, layer_names),
            '    }',
            '  }',
            '}',
            f'if (ec!={len(elements)} || pc!={len(pad_report)}) issue="Nombre de composants ou de pads different de la source.";',
            'if (issue!="") { dlgMessageBox("Import annule : " + issue); exit(1); }',
            '// Generation du SCR. La compatibilite CLI des constantes reste a valider dans Fusion.',
            'output(scriptPath, "wt") {'])
        if replacements:
            validation = guard(replacements)
            idx = ulp.index('if (issue!="") { dlgMessageBox("Import annule : " + issue); exit(1); }')
            ulp[idx:idx] = validation + [
                'if (argc>1 && argv[1]=="--validate") { output(filesetext(argv[0], ".validation.txt"), "wt") { printf("%s", issue); } exit(0); }']
            # Le SCR public relance les contrôles. Le corps privé dépend du groupe
            # fraîchement établi et ne doit jamais être lancé seul.
            ulp[-1:-1] = ['string launcherPath=scriptPath;', 'scriptPath=filesetext(argv[0], ".body.scr");']
        if inner_layers:
            # Insérer les contrôles AVANT la création du SCR, sans action PCB.
            guard = ['int layerFound = 0;']
            for number in inner_layers:
                lname = q(layer_names[number])
                guard.extend(['layerFound = 0;', f'board(B) {{ B.layers(L) if (L.name == {lname}) layerFound++; }}',
                              f'if (layerFound != 1) {{ dlgMessageBox("Couche interne absente ou ambigue : " + {lname}); exit(1); }}'])
            ulp[-1:-1] = guard
        for line in lines:
            variables = re.findall(r'@(TOP|BOTTOM|PADS|VIAS|VIA_BOTTOM)@', line)
            if variables:
                template = re.sub(r'@(TOP|BOTTOM|PADS|VIAS|VIA_BOTTOM)@', lambda m: '%d' if m[1]=='VIA_BOTTOM' else '%s', line.replace('%', '%%'))+'\n'
                names = {'TOP':'top','BOTTOM':'bottom','PADS':'padsLayer','VIAS':'viasLayer','VIA_BOTTOM':'viaBottom'}
                ulp.append('  printf('+q(template)+', '+', '.join(names[v] for v in variables)+');')
            else:
                ulp.append('  printf("%s", '+q(line+'\n')+');')
        ulp.extend(['}', 'if (fileerror()) { dlgMessageBox("Erreur lors de la creation du script."); exit(1); }'])
        if replacements:
            # Le lanceur peut être le SCR actif : Fusion refuse alors sa réécriture.
            # Sortir du passage --apply avant toute ouverture du lanceur en écriture,
            # après les contrôles et la génération du corps à appliquer.
            ulp.extend([
                'if (argc>1 && argv[1]=="--apply") {',
                '  board(B) { clrgroup(B); B.signals(S) { S.polyPours(P) { P.wires(W) setgroup(W); } } }',
                '''  exit("SCRIPT '" + scriptPath + "';");''', '}',
                'output(launcherPath, "wt") {',
                '''    printf("RUN '%s' --apply;\\n", argv[0]);''', '}',
                'if (fileerror()) { dlgMessageBox("Erreur lanceur."); exit(1); }',
                'scriptPath=launcherPath;'])
        if args.confirm_apply and not inner_layers:
            # Le clic KiCad prépare seulement le livrable ; la décision d'appliquer
            # appartient au dialogue Fusion, après contrôle de la carte cible.
            # Fermer, Échap et Entrée conservent le script sans modifier le PCB.
            ulp.extend([
                'if (dlgMessageBox("Controles termines. Appliquer les pistes et vias sur le PCB ouvert ?\\nLe routage des nets concernes sera remplace.\\nScript : " + scriptPath, "Appliquer les pistes et vias", "+Conserver sans appliquer") == 0) {',
                '''  exit("SCRIPT '" + scriptPath + "';");''',
                '}',
                'exit(0);'])
        elif args.prepare_only or inner_layers:
            if not args.quiet_prepare:
                ulp.append('dlgMessageBox("Script genere sans application : " + scriptPath);')
            ulp.append('exit(0);')
        else:
            ulp.append('exit("SCRIPT \'" + scriptPath + "\';");')
        (args.output/report['ulp_filename']).write_text('\n'.join(ulp)+'\n', encoding='ascii')
        # Copie de lecture uniquement, avec marqueurs de couche : jamais livrée en .scr.
        (args.output/'commandes_audit.txt').write_text('\n'.join(lines)+'\n', encoding='ascii')
        report['replaced_nets'] = involved
        (args.output/'rapport_conversion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
