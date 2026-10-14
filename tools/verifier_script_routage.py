"""Relit le corps printf de l'ULP et compare ses commandes aux objets KiCad.

Ce contrôle hors Fusion détecte les pertes/duplications, les erreurs d'unités,
de couche, de net, de largeur et de perçage dans le fichier réellement livré.
Il ne remplace ni la compilation ULP par Fusion ni son DRC.
"""
import collections
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

try:
    from .kicad_routage_vers_fusion import parse, nodes, field, net
except ImportError:
    from kicad_routage_vers_fusion import parse, nodes, field, net


def verify_conservation(source, delivered, links):
    """La normalisation doit conserver les axes source, plus les seuls ponts.

    Comparer les unions par net/couche/largeur permet les découpages et les
    regroupements colinéaires sans faire confiance à fusion_segments.
    """
    from shapely.geometry import LineString
    from shapely.ops import unary_union
    def groups(wires):
        result = collections.defaultdict(list)
        for w in wires:
            result[(w['net'],w['layer'],round(w['width'],6))].append(LineString([w['start'],w['end']]))
        return {k:unary_union(v) for k,v in result.items()}
    expected, actual = groups(source+links),groups(delivered)
    assert expected.keys()==actual.keys(), 'Groupes cuivre source perdus ou ajoutés'
    for k,a in expected.items():
        b = actual[k]
        assert a.buffer(.000002).covers(b) and b.buffer(.000002).covers(a), f'Cuivre source perdu ou ajouté : {k}'


def check(folder):
    report = json.loads((folder/'rapport_conversion.json').read_text(encoding='utf-8'))
    for key, path in [('sha256_kicad','source_kicad'),('sha256_brd','source_brd')]:
        assert hashlib.sha256(Path(report[path]).read_bytes()).hexdigest()==report[key], 'Source modifiée depuis la conversion'
    source = parse(Path(report['source_kicad']).read_text(encoding='utf-8-sig'))
    code = (folder/report.get('ulp_filename','cube_pcb1_pistes_vias.ulp')).read_text(encoding='ascii')
    try:
        from .polygon_replacement import inventory, guard
    except ImportError:
        from polygon_replacement import inventory, guard
    replacements = inventory(ET.parse(report['source_brd']).find('./drawing/board'), report['zones'],
                             {int(k):v for k,v in report['source_layer_names'].items()})
    assert report.get('replaced_polygons', []) == replacements, 'Inventaire des polygones à remplacer altéré'
    if replacements:
        assert '\n'.join(guard(replacements)) in code, 'Protection des polygones cible altérée'
        assert 'board(B) { clrgroup(B); B.signals(S) { S.polyPours(P) { P.wires(W) setgroup(W); } } }' in code, 'Sélection des polygones altérée'
        assert 'if (argc>1 && argv[1]=="--apply") {' in code
        assert '''printf("RUN '%s' --apply;\\n", argv[0]);''' in code, 'Revalidation à application absente'
    dx = float(report['transform']['x_fusion'].split(' - ')[1])
    sy = float(report['transform']['y_fusion'].split(' - ')[0])
    mapping = report['net_name_mapping']
    inverse = {v:k for k,v in mapping.items()}

    def coords(x, y):
        return round(float(x)+dx, 6), round(sy-float(y), 6)

    def wire_key(n, l, width, start, end):
        return n, l, round(float(width),6), tuple(sorted((tuple(round(float(x),6) for x in start), tuple(round(float(x),6) for x in end))))

    expected_wires = collections.Counter(wire_key(net(s), field(s,'layer')[0], field(s,'width')[0], field(s,'start'), field(s,'end')) for s in nodes(source,'segment'))
    layers = {v:k for k,v in report['layer_mapping'].items()}
    original_report = collections.Counter(wire_key(inverse.get(w['net'],w['net']), layers[w['layer']], w['width'], coords(*w['start']),coords(*w['end'])) for w in report['segments'])
    assert original_report == expected_wires, 'Source et rapport différents'
    if 'fusion_segments' in report:
        verify_conservation(report['segments'],report['fusion_segments'],report['junctions'])
        import math
        try:
            from .regles_fusion_kicad import effective_widths, ulp_class_guard
        except ImportError:
            from regles_fusion_kicad import effective_widths, ulp_class_guard
        board = ET.parse(report['source_brd']).find('./drawing/board')
        widths, _ = effective_widths(board)
        if 'width_rules' in report:
            assert report['width_rules']==widths, 'Règles de largeur du rapport incohérentes'
            assert '\n'.join(ulp_class_guard(board)) in code, 'Protection des classes altérée'
        try:
            from .cuivre_jonctions import ulp_pad_guard
        except ImportError:
            from cuivre_jonctions import ulp_pad_guard
        expected_guard = '\n'.join(ulp_pad_guard(report['pads'], {int(k):v for k,v in report['source_layer_names'].items()}))
        assert expected_guard in code, 'Protection du cuivre des pads altérée'
        # Les contacts non résolus sont des avertissements. Vérifier toujours
        # les commandes livrées et les raccords effectivement ajoutés.
        try:
            from .jonctions_fusion import projection
        except ImportError:
            from jonctions_fusion import projection
        for link in report['junctions']:
            a = link['anchor']
            assert link['net'] == a['net'] and link['layer'] in a['layers']
            assert math.dist(link['start'],a['at'])<=0.000002, 'Raccord éloigné du centre'
            if 'endpoint_wire' in link:
                w = link['endpoint_wire']
                assert math.dist(link['end'],projection(link['end'],w['start'],w['end']))<=0.000002, 'Raccord hors axe cible'
            if a.get('source')=='track_endpoint':
                assert a['source_wire'] in report['segments']
                assert a['at'] in (a['source_wire']['start'],a['source_wire']['end'])
                assert a['radius']==a['source_wire']['width']/2
            minimum = widths[link['net']]['minimum']
            assert link['width'] > 0 and math.isfinite(link['width']), 'Largeur invalide'
            # L'exception utilisateur ne concerne que les capsules déjà
            # recouvertes. La preuve complète est obligatoire sous le minimum.
            if link['width'] < minimum-0.000002:
                assert link.get('covered_neckdown') and link.get('geometry_proof'), 'Raccord sous minimum sans preuve'
                assert abs(link['minimum_width']-minimum)<0.000002, 'Minimum de raccord incohérent'
            if link.get('geometry_proof'):
                try:
                    from .cuivre_jonctions import contained, proof_geometries
                except ImportError:
                    from cuivre_jonctions import contained, proof_geometries
                if 'endpoint_wire' in link:
                    assert link['endpoint_wire'] in report['segments']
                    assert (link['endpoint_wire']['net'],link['endpoint_wire']['layer']) == (link['net'],link['layer'])
                if 'second_anchor' in link:
                    assert link['second_anchor']['net'] == link['net']
                    assert link['layer'] in link['second_anchor']['layers']
                assert contained(link, proof_geometries(link)), 'Liaison hors formes cuivre'
            elif 'container_wire' in link:
                try:
                    from .jonctions_fusion import projection
                except ImportError:
                    from jonctions_fusion import projection
                w = link['container_wire']
                assert w in report['segments']
                assert (w['net'],w['layer'])==(link['net'],link['layer'])
                assert all(math.dist(p,projection(p,w['start'],w['end']))+link['width']/2 <= w['width']/2+0.000002 for p in (link['start'],link['end'])), 'Liaison hors piste'
            elif 'second_anchor' in link:
                b = link['second_anchor']
                assert a['net']==b['net'] and link['layer'] in b['layers']
                assert math.dist(a['at'],b['at'])/2+link['width']/2 <= min(a['radius'],b['radius'])+0.000002, 'Liaison hors des deux disques'
            else:
                # La capsule tient dans l'union du disque d'ancrage et du
                # disque garanti au point de raccordement de la piste.
                w = link['endpoint_wire']
                assert w in report['segments'] and (w['net'],w['layer'])==(link['net'],link['layer'])
                assert link['width'] <= w['width']
                try:
                    from .jonctions_fusion import projection, covered
                except ImportError:
                    from jonctions_fusion import projection, covered
                assert math.dist(link['start'],a['at']) <= 0.000002
                assert math.dist(link['end'],projection(link['end'],w['start'],w['end'])) <= 0.000002
                assert covered(math.dist(link['start'],link['end']),link['width'],a['radius'],w['width']/2), 'Liaison hors cuivre'
                if a.get('source') == 'track_endpoint':
                    assert a['source_wire'] in report['segments']
                    assert a['at'] in (a['source_wire']['start'],a['source_wire']['end'])
                    assert a['radius'] == a['source_wire']['width']/2
        expected_wires = collections.Counter(wire_key(inverse.get(w['net'],w['net']), layers[w['layer']], w['width'], coords(*w['start']),coords(*w['end'])) for w in report['fusion_segments'])
    expected_vias = collections.Counter((net(v), tuple(round(float(x),6) for x in field(v,'at')), float(field(v,'size')[0]), float(field(v,'drill')[0])) for v in nodes(source,'via'))
    # Relire les contours depuis KiCad, indépendamment de la liste du rapport.
    expected_zones = collections.Counter()
    for z in nodes(source, 'zone'):
        vertices = tuple(tuple(round(float(v),6) for v in p[1:]) for p in field(nodes(z,'polygon')[0],'pts'))
        cp = nodes(z,'connect_pads')[0]
        expected_zones[(net(z), field(z,'layer')[0], float(field(z,'min_thickness')[0]),
                        float(field(cp,'clearance')[0]), field(nodes(z,'fill')[0],'island_removal_mode') == ['1'], vertices)] += 1
    results = []
    modern_code = 'int viaBottom' in code
    for bottom in ((16,304) if modern_code else (16,64,304)):
        modern = modern_code and bottom == 304
        constants = {'top':'ct' if modern else 1,'bottom':'cb' if modern else bottom,
                     'padsLayer':'cp' if modern else 17,'viasLayer':'cv' if modern else 18,
                     'viaBottom':64 if modern else bottom}
        commands = []
        for line in code.splitlines():
            if not line.startswith('  printf('):
                continue
            args = line[len('  printf('):-2]
            form, end = json.JSONDecoder().raw_decode(args)
            rest = args[end:].lstrip(', ')
            if form == '%s':
                commands.extend(json.loads(rest).splitlines())
            else:
                commands.extend((form % tuple(constants[v.strip()] for v in rest.split(','))).splitlines())
        actual_wires, actual_vias = collections.Counter(), collections.Counter()
        actual_zones, polygon_settings = collections.Counter(), {}
        layer, drill = None, None
        ripped = []
        deletions = 0
        for line in commands:
            if line.startswith('#'):
                continue
            if line == 'DELETE (>0 0);':
                assert replacements and not ripped and not actual_wires and not actual_vias and not actual_zones
                deletions += 1
                continue
            match = re.fullmatch(r"LINE '([^']+)' ([\d.]+) \(([-\d.]+) ([-\d.]+)\) \(([-\d.]+) ([-\d.]+)\);", line)
            if match:
                name,width,x1,y1,x2,y2 = match.groups()
                actual_wires[wire_key(inverse.get(name,name), layer, width, coords(x1,y1),coords(x2,y2))] += 1
                continue
            match = re.fullmatch(r"VIA '([^']+)' ([\d.]+) ROUND (\d+)-(\d+)(?: STOP)? \(([-\d.]+) ([-\d.]+)\);", line)
            if match:
                name,size,top,end,x,y = match.groups()
                assert int(top)==1 and int(end)==constants['viaBottom']
                actual_vias[(inverse.get(name,name),coords(x,y),float(size),drill)] += 1
                continue
            match = re.fullmatch(r"POLYGON '([^']+)' ([\d.]+) ((?:\([-\d.]+ [-\d.]+\) ?)+);", line)
            if match:
                name, width, points = match.groups()
                pts = tuple(coords(x,y) for x,y in re.findall(r'\(([-\d.]+) ([-\d.]+)\)',points))
                assert len(pts) >= 4 and pts[0] == pts[-1], 'Polygone non fermé'
                assert polygon_settings.get('ORPHANS') in ('ON','OFF')
                assert polygon_settings == {'POUR':'SOLID', 'THERMALS':'OFF', 'ORPHANS':polygon_settings['ORPHANS'], 'RANK':'1', 'ISOLATE':polygon_settings.get('ISOLATE')}, 'Paramètres de polygone absents ou différents'
                actual_zones[(inverse.get(name,name),layer,float(width),float(polygon_settings['ISOLATE']),polygon_settings['ORPHANS']=='ON',pts[:-1])] += 1
                continue
            match = re.fullmatch(r'CHANGE (POUR|THERMALS|ORPHANS|RANK|ISOLATE) ([\w.]+);',line)
            if match:
                polygon_settings[match[1]] = match[2]
                continue
            if line.startswith('LAYER '):
                selected = line[6:-1]
                if selected.startswith("'"):
                    names = report['source_layer_names']
                    layer = next(k for k, n in report['layer_mapping'].items() if names[str(n)] == selected[1:-1])
                else:
                    layer = {str(constants['top']):'F.Cu',str(constants['bottom']):'B.Cu'}[selected]
            elif line.startswith('CHANGE DRILL '):
                drill = float(line[13:-1])
            elif line.startswith('RIPUP '):
                ripped.append(re.fullmatch(r"RIPUP '([^']+)';",line)[1])
            else:
                display='DISPLAY '+ ' '.join(str(constants[k]) for k in ('top','bottom','padsLayer','viasLayer'))+';'
                assert line in ('GRID MM;',display, 'SET WIRE_BEND 2;', 'CHANGE STYLE Continuous;', 'CHANGE STOP OFF;', 'SET POLYGON_RATSNEST ON;', 'RATSNEST;'), line
        assert actual_wires == expected_wires, 'Segments différents après relecture du livrable'
        assert actual_vias == expected_vias, 'Vias différents après relecture du livrable'
        assert actual_zones == expected_zones, 'Zones différentes après relecture du livrable'
        if expected_zones:
            assert 'SET POLYGON_RATSNEST ON;' in commands and commands[-1] == 'RATSNEST;'
            assert code.index('S.polyPours(P)') < code.index('output(scriptPath'), 'Protection contre les doublons absente'
        assert ripped == report['replaced_nets']
        assert deletions == int(bool(replacements)), 'Suppression des anciens polygones absente ou dupliquée'
        results.append({'bottom_layer_simulated':bottom,'segments_exact':sum(actual_wires.values()),'vias_exact':sum(actual_vias.values()),'zone_contours_exact':sum(actual_zones.values()),'nets_replaced':len(ripped)})
    result = {'checks_passed':True, 'checks':results, 'fusion_executed':False, 'drc_executed':False,
              'warnings':report.get('warnings', []),
              'unresolved_junctions_count':len(report.get('unresolved_junctions', []))}
    (folder/'verification_script.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    import sys
    check(Path(sys.argv[1]))
