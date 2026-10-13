"""Géométrie conservatrice des jonctions, sans ajout hors cuivre existant.

Shapely effectue les opérations booléennes ; les formes sérialisables restent
dans le rapport pour permettre la relecture du certificat de chaque liaison.
"""
import math
import json
from functools import lru_cache

try:
    from shapely.affinity import rotate, translate
    from shapely.geometry import Point, LineString, Polygon, box
    from shapely.ops import unary_union
except ImportError as exc:
    raise RuntimeError('Le contrôle des jonctions nécessite Shapely 2 dans le Python exécutant le plugin.') from exc

TOL = 0.000002  # mm : même tolérance que les coordonnées du convertisseur.
RES = 512


def shape(spec):
    return _shape(json.dumps(spec, sort_keys=True))


@lru_cache(maxsize=4096)
def _shape(serialized):
    spec = json.loads(serialized)
    kind = spec['kind']
    if kind == 'circle':
        geom = Point(0, 0).buffer(spec['diameter']/2, quad_segs=RES)
    elif kind == 'octagon':
        r = spec['diameter']/2
        geom = Polygon([(r*math.cos(math.pi/8+i*math.pi/4),
                         r*math.sin(math.pi/8+i*math.pi/4)) for i in range(8)])
    else:
        x, y = spec['size']
        r = spec.get('radius', 0)
        if r:
            geom = box(-x/2+r, -y/2+r, x/2-r, y/2-r).buffer(r, quad_segs=RES)
            # Une capsule peut avoir une partie droite de largeur nulle.
            if abs(x-2*r) < 1e-12 or abs(y-2*r) < 1e-12:
                geom = LineString([(-x/2+r, -y/2+r),
                                   (x/2-r, y/2-r)]).buffer(r, quad_segs=RES)
        else:
            geom = box(-x/2, -y/2, x/2, y/2)
    return translate(rotate(geom, spec.get('angle', 0), origin=(0, 0)),
                     *spec['at'])


def anchor_shape(anchor, layer):
    specs = anchor.get('copper', {}).get(str(layer))
    if not specs:
        return shape(dict(kind='circle',at=anchor['at'],diameter=anchor['radius']*2))
    # Garantir l'absence d'ajout à la fois dans KiCad et dans Fusion. Une
    # différence de taille existante ne donne pas autorisation de l'agrandir.
    return _common(json.dumps(specs, sort_keys=True))


@lru_cache(maxsize=4096)
def _common(serialized):
    specs = json.loads(serialized)
    return shape(specs[0]).intersection(shape(specs[1]))


def wire_shape(wire, outer=False):
    return _wire(tuple(wire['start']), tuple(wire['end']), wire['width'], outer)


@lru_cache(maxsize=8192)
def _wire(start, end, width, outer):
    radius = width/2
    # Le buffer polygonal inscrit sous-estime un arrondi. Pour la liaison à
    # vérifier, prendre au contraire une enveloppe circonscrite ; les objets
    # de référence restent inscrits. TOL couvre seulement l'arrondi numérique.
    if outer:
        radius /= math.cos(math.pi/(4*RES))
    return LineString([start, end]).buffer(radius, quad_segs=RES)


def contained(link, geometries):
    return unary_union(geometries).buffer(TOL).covers(wire_shape(link, outer=True))


def touches(a, b):
    """Écarter les boîtes disjointes avant la distance entre les arrondis fins."""
    ax, ay, bx, by = a.bounds
    cx, cy, dx, dy = b.bounds
    if bx + TOL < cx or dx + TOL < ax or by + TOL < cy or dy + TOL < ay:
        return False
    return a.distance(b) <= TOL


def proof_geometries(link):
    layer = link['layer']
    geometries = [anchor_shape(link['anchor'], layer)]
    if 'second_anchor' in link:
        geometries.append(anchor_shape(link['second_anchor'], layer))
    if 'endpoint_wire' in link:
        geometries.append(wire_shape(link['endpoint_wire']))
    return geometries


def ulp_pad_guard(pads, layer_names):
    """Contrôler le cuivre effectif avant d'appliquer une preuve géométrique.

    Les règles Fusion peuvent changer entre export et RUN. Les noms de couche
    évitent de confondre les index XML historiques avec les index Fusion V2.
    Le contexte appelant fournit B (board), C (contact), key et issue.
    """
    lines = []
    for pad in pads:
        copper = pad.get('junction_copper', {})
        if not copper:
            continue
        key = json.dumps(pad['component']+'/'+pad['pad'])
        lines.append(f'      if (key=={key}) {{')
        for layer, (_, target) in copper.items():
            lname = json.dumps(layer_names[int(layer)])
            angle = target.get('angle', 0) % 360
            if 'diameter' in target:
                kind = {'circle':'ROUND','rect':'SQUARE','octagon':'OCTAGON'}[target['kind']]
                check = (f'!C.pad || abs(u2mm(C.pad.diameter[L.number])-{target["diameter"]:.9f})>0.000002 '
                         f'|| C.pad.shape[L.number]!=PAD_SHAPE_{kind} '
                         f'|| abs(C.pad.angle-{angle:.9f})>0.0001')
            else:
                x, y = target['size']
                rounding = 200*target.get('radius',0)/min(x,y)
                check = (f'!C.smd || C.smd.layer!=L.number '
                         f'|| abs(u2mm(C.smd.dx[L.number])-{x:.9f})>0.000002 '
                         f'|| abs(u2mm(C.smd.dy[L.number])-{y:.9f})>0.000002 '
                         f'|| abs(C.smd.roundness-{rounding:.9f})>0.0001 '
                         f'|| abs(C.smd.angle-{angle:.9f})>0.0001')
            lines.append(f'        B.layers(L) if (L.name=={lname}) {{ if ({check}) issue="Cuivre du pad different : " + key; }}')
        lines.append('      }')
    return lines
