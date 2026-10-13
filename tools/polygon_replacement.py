"""Remplacement borné aux polygones rectangulaires identifiés dans la source.

La suppression utilise un groupe constitué par objets ULP, jamais un clic dont
la sélection pourrait viser une piste voisine. Le groupe est créé seulement
après revalidation de la cible, immédiatement avant l'exécution du corps SCR.
"""
import json
import math


def inventory(board, zones, layer_names):
    result = []
    destinations = {(z['net'], z['layer']) for z in zones}
    occupied = set()
    for signal in board.findall('./signals/signal'):
        for p in signal.findall('polygon'):
            layer = int(p.get('layer'))
            key = (signal.get('name'), layer)
            assert key in destinations, 'Polygone source sans zone de remplacement sur le même net et la même couche'
            assert layer not in occupied, 'Plusieurs polygones source sur une même couche'
            occupied.add(layer)
            pts = [(float(v.get('x')), float(v.get('y'))) for v in p]
            assert len(pts) == 4 and all(v.tag == 'vertex' and float(v.get('curve', '0')) == 0 for v in p), 'Remplacement : rectangle source sans arc requis'
            assert all(math.isfinite(c) for xy in pts for c in xy), 'Contour source invalide'
            xs, ys = sorted(set(x for x,y in pts)), sorted(set(y for x,y in pts))
            assert len(xs) == len(ys) == 2 and len(set(pts)) == 4, 'Remplacement : rectangle source requis'
            assert all((a[0] == b[0]) != (a[1] == b[1]) for a,b in zip(pts, pts[1:]+pts[:1])), 'Contour source croisé'
            width = float(p.get('width'))
            assert math.isfinite(width) and width > 0, 'Largeur de polygone source invalide'
            result.append(dict(net=key[0], layer=layer, layer_name=layer_names[layer],
                               width=width, bounds=[xs[0],ys[0],xs[1],ys[1]]))
    return result


def guard(polygons):
    """Code ULP sans modification du design ; tous les polygones sont comptés.

    L'export EAGLE utilise la ligne médiane, Fusion moderne le bord extérieur.
    Pour un rectangle, le second contour est exactement dilaté de width/2.
    Les deux représentations sont acceptées, pas une tolérance arbitraire.
    """
    q = lambda s: json.dumps(s, ensure_ascii=True)
    code = ['int polygonCount=0;', 'int polygonSeen[];', 'board(B) { B.signals(S) { S.polyPours(P) {',
            '  polygonCount++;', '  int match=-1;', '  string lname="";',
            '  B.layers(L) if (L.number==P.layer) lname=L.name;']
    for i,p in enumerate(polygons):
        code.append(f'  if (S.name=={q(p["net"])} && lname=={q(p["layer_name"])}) match={i};')
    code += ['  if (match<0) issue="Polygone cible inattendu (net/couche).";', '  else {',
             '    polygonSeen[match]++;', '    int edges=0, straight=1;',
             '    real xmin=1e9,ymin=1e9,xmax=-1e9,ymax=-1e9;',
             '    P.wires(W) {', '      edges++;',
             '      if (W.curve!=0 || (W.x1!=W.x2 && W.y1!=W.y2)) straight=0;',
             '      xmin=min(xmin,min(u2mm(W.x1),u2mm(W.x2))); ymin=min(ymin,min(u2mm(W.y1),u2mm(W.y2)));',
             '      xmax=max(xmax,max(u2mm(W.x1),u2mm(W.x2))); ymax=max(ymax,max(u2mm(W.y1),u2mm(W.y2)));',
             '    }', '    int geometry=0;']
    for i,p in enumerate(polygons):
        alternatives = []
        for offset in (0, p['width']/2):
            bounds = [p['bounds'][0]-offset,p['bounds'][1]-offset,p['bounds'][2]+offset,p['bounds'][3]+offset]
            alternatives.append('('+' && '.join(f'abs({name}-({v:.9f}))<0.00001' for name,v in zip(('xmin','ymin','xmax','ymax'),bounds))+')')
        code.append(f'    if (match=={i} && abs(u2mm(P.width)-{p["width"]:.9f})<0.00001 && ('+' || '.join(alternatives)+')) geometry=1;')
    code += ['    if (edges!=4 || !straight || !geometry) issue="Contour de polygone cible different de la source.";',
             '  }', '} } }', f'if (polygonCount!={len(polygons)}) issue="Nombre de polygones cible different de la source.";']
    for i in range(len(polygons)):
        code.append(f'if (polygonSeen[{i}]!=1) issue="Polygone source absent ou duplique dans la cible.";')
    return code
