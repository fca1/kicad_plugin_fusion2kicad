"""Jonctions explicites sans extension du cuivre : disques inscrits et axes.

Une liaison est admise seulement si sa capsule est contenue dans le cuivre
garanti des objets source. Les pistes sont découpées au raccordement.
Les cas non prouvés restent inchangés ; ce module n'est pas un autorouteur.
"""
import math
try:
    from .cuivre_jonctions import anchor_shape, wire_shape, contained, proof_geometries, touches
except ImportError:
    from cuivre_jonctions import anchor_shape, wire_shape, contained, proof_geometries, touches

EPS = 0.000002


def point(p):
    return tuple(round(float(x), 6) for x in p)


def projection(p, a, b):
    d = [b[i]-a[i] for i in (0, 1)]
    den = sum(x*x for x in d)
    t = max(0., min(1., sum((p[i]-a[i])*d[i] for i in (0, 1))/den))
    return point([a[i]+t*d[i] for i in (0, 1)])


def normalize(wires, anchors, unresolved=None):
    anchors = list(anchors)
    minima = {a['net']: a.get('min_width', .1524) for a in anchors}
    # Une extrémité de piste possède elle aussi un disque cuivre garanti.
    # Cela couvre les raccords décalés sans changer leur couche ou leur net.
    anchors += [dict(net=w['net'], at=p, radius=w['width']/2,
                     layers=[w['layer']], min_width=minima.get(w['net'],w['width']),
                     source='track_endpoint', source_wire=w)
                for w in wires for p in (w['start'],w['end'])]
    cuts = [{point(w['start']), point(w['end'])} for w in wires]
    links, seen = [], set()
    for a in anchors:
        center = point(a['at'])
        for i, w in enumerate(wires):
            if a['net'] != w['net'] or w['layer'] not in a['layers']:
                continue
            q = projection(center, w['start'], w['end'])
            distance = math.dist(center, q)
            if distance <= EPS:
                cuts[i].add(center)
                continue
            # La largeur est conservée : pas de petit fil sous le minimum DRC.
            width = max(w['width'], a.get('min_width', w['width']))
            container = None
            if not covered(distance, width, a['radius'], w['width']/2):
                # Le centre du via peut être à l'intérieur d'une grosse piste.
                # Une liaison respectant la classe tient alors dans la capsule
                # de cette piste, même si elle ne tient pas dans le petit via.
                width = a.get('min_width', w['width'])
                if not covered(distance, width, a['radius'], w['width']/2):
                    if distance + width/2 <= w['width']/2 - EPS:
                        container = w
                    else:
                        # Les vrais rectangles et l'axe entier de la piste
                        # couvrent des contacts que les deux disques ratent.
                        if not touches(anchor_shape(a,w['layer']),wire_shape(w)): continue
                        candidate = dict(net=w['net'], layer=w['layer'], width=width,
                                         start=list(center), end=list(q), anchor=a,
                                         endpoint_wire=w, geometry_proof=True)
                        if not contained(candidate, proof_geometries(candidate)):
                            # Le projeté le plus proche n'est pas toujours le
                            # meilleur raccord : une arrivée légèrement plus
                            # loin sur l'axe peut éviter le bord du cuivre.
                            # Chaque essai garde la même preuve de containment.
                            length = math.dist(w['start'],w['end'])
                            unit = [(w['end'][j]-w['start'][j])/length for j in (0,1)]
                            alternatives = {projection([q[j]+sign*step*unit[j] for j in (0,1)],
                                                       w['start'],w['end'])
                                            for step in (.001,.005,.01,.02,.05,.1,.2,.5)
                                            for sign in (-1,1)}
                            accepted = None
                            for trial in sorted(alternatives,key=lambda p:math.dist(center,p)):
                                candidate['end'] = list(trial)
                                if contained(candidate, proof_geometries(candidate)):
                                    accepted = trial
                                    break
                            if accepted is None: continue
                            q = accepted
                            distance = math.dist(center,q)
            key = (a['net'], w['layer'], center, q)
            cuts[i].add(q)
            if key not in seen:
                seen.add(key)
                link = dict(net=w['net'], layer=w['layer'], width=width,
                            start=list(center), end=list(q), anchor=a, endpoint_wire=w)
                if container: link['container_wire'] = container
                elif not covered(distance, width, a['radius'], w['width']/2):
                    link['geometry_proof'] = True
                links.append(link)
    # Deux pastilles/vias de même net peuvent se recouvrir sans centres liés.
    # Chaque demi-capsule doit tenir dans son disque ; c'est plus restrictif
    # qu'une simple intersection visuelle, donc aucun saut dans l'air possible.
    for i, a in enumerate(anchors):
        if a.get('source') == 'track_endpoint': continue
        for b in anchors[:i]:
            if b.get('source') == 'track_endpoint': continue
            if a['net'] != b['net']: continue
            distance = math.dist(a['at'],b['at'])
            if distance <= EPS: continue
            for layer in set(a['layers']) & set(b['layers']):
                widths = [w['width'] for w in wires if w['net']==a['net'] and w['layer']==layer]
                if not widths: continue
                width = min(widths)
                width = max(min(widths), a.get('min_width', min(widths)),
                            b.get('min_width', min(widths)))
                candidate = dict(net=a['net'],layer=layer,width=width,
                                  start=list(point(a['at'])),end=list(point(b['at'])),
                                  anchor=a,second_anchor=b)
                if distance/2+width/2 > min(a['radius'],b['radius'])-EPS:
                    if not touches(anchor_shape(a,layer),anchor_shape(b,layer)): continue
                    candidate['geometry_proof'] = True
                    if not contained(candidate, proof_geometries(candidate)): continue
                links.append(candidate)
    # Une intersection d'axes doit être un sommet partagé, même si elle se
    # situe au milieu des deux segments (Fusion ne déduit pas tous les contacts).
    for i, a in enumerate(wires):
        p, q = a['start'], a['end']
        u = [q[j]-p[j] for j in (0, 1)]
        for j in range(i):
            b = wires[j]
            if (a['net'], a['layer']) != (b['net'], b['layer']):
                continue
            r, s = b['start'], b['end']
            v = [s[k]-r[k] for k in (0, 1)]
            cross = u[0]*v[1]-u[1]*v[0]
            if abs(cross) < 1e-12:
                for x in (p, q):
                    if math.dist(x, projection(x, r, s)) < EPS: cuts[j].add(point(x))
                for x in (r, s):
                    if math.dist(x, projection(x, p, q)) < EPS: cuts[i].add(point(x))
                continue
            delta = [r[k]-p[k] for k in (0, 1)]
            t = (delta[0]*v[1]-delta[1]*v[0])/cross
            z = (delta[0]*u[1]-delta[1]*u[0])/cross
            if 0 <= t <= 1 and 0 <= z <= 1:
                x = point([p[k]+t*u[k] for k in (0, 1)])
                cuts[i].add(x); cuts[j].add(x)
    result = []
    for w, pts in zip(wires, cuts):
        ordered = sorted(pts, key=lambda p: math.dist(w['start'], p))
        for a, b in zip(ordered, ordered[1:]):
            if math.dist(a, b) > EPS:
                result.append(dict(w, start=list(a), end=list(b)))
    # Ne conserver qu'une forêt des raccordements nécessaires. Les axes
    # source restent tous présents ; seuls les nouveaux ponts redondants sont
    # supprimés. Un via/pad traversant relie ses centres sur toutes ses couches.
    parent = {}
    def find(k):
        parent.setdefault(k,k)
        if parent[k] != k: parent[k] = find(parent[k])
        return parent[k]
    def join(a,b):
        a,b = find(a),find(b)
        if a == b: return False
        parent[a] = b
        return True
    def key(w,p): return (w['net'],w['layer'],*point(p))
    for w in result: join(key(w,w['start']),key(w,w['end']))
    for a in anchors:
        keys = [(a['net'],layer,*point(a['at'])) for layer in a['layers']]
        for k in keys[1:]: join(keys[0],k)
    links = [w for w in sorted(links,key=lambda w:(
        w['width'] < w['anchor'].get('min_width',0), math.dist(w['start'],w['end'])))
        if join(key(w,w['start']),key(w,w['end']))]
    result.extend({k:v for k,v in link.items() if k not in ('anchor','second_anchor','container_wire','endpoint_wire','geometry_proof')} for link in links)
    # Un contact physique sans connexion des sommets n'est pas un succès.
    # Ne demander que les contacts réellement existants, sans relier deux
    # îlots éloignés d'un même net ni supposer que les plans les réunissent.
    pending, seen_contacts = [], set()
    real_anchors = [a for a in anchors if a.get('source') != 'track_endpoint']
    for a in real_anchors:
        for layer in a['layers']:
            geom = anchor_shape(a, layer)
            ka = (a['net'], layer, *point(a['at']))
            for w in wires:
                if (a['net'], layer) != (w['net'], w['layer']): continue
                if find(ka) == find(key(w, w['start'])): continue
                if not touches(geom,wire_shape(w)): continue
                ident = (ka, find(key(w, w['start'])))
                if ident in seen_contacts: continue
                seen_contacts.add(ident)
                pending.append(dict(net=a['net'], layer=layer, anchor=a,
                                    wire=w, reason='Contact cuivre sans raccord contenu à la largeur minimale'))
    for i, a in enumerate(real_anchors):
        for b in real_anchors[:i]:
            if a['net'] != b['net']: continue
            for layer in set(a['layers']) & set(b['layers']):
                if find((a['net'],layer,*point(a['at']))) == find((b['net'],layer,*point(b['at']))): continue
                if not touches(anchor_shape(a,layer),anchor_shape(b,layer)): continue
                pending.append(dict(net=a['net'],layer=layer,anchor=a,second_anchor=b,
                                    reason='Contact entre pads/vias sans raccord contenu à la largeur minimale'))
    if unresolved is not None:
        unresolved.extend(pending)
    elif pending:
        raise ValueError(f'{len(pending)} contact(s) cuivre sans jonction explicite ; aucune extension automatique du cuivre.')
    return result, links


def covered(distance, width, radius_a, radius_b):
    """Capsule entre les centres contenue dans l'union de deux disques.

    À la demi-largeur h, les cordes des disques doivent se recouvrir.
    C'est la section la plus restrictive du rectangle ; ses caps tiennent
    dans les disques. Aucun cuivre n'est ajouté hors de cette union.
    """
    h = width/2
    if h > min(radius_a, radius_b): return False
    return (math.sqrt(max(0,radius_a**2-h**2)) +
            math.sqrt(max(0,radius_b**2-h**2)) >= distance-EPS/2)
