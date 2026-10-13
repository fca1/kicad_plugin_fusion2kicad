"""Transfert explicite des règles EAGLE vers une copie de projet KiCad 10.

Les nets sont appariés par leurs contacts, jamais par une ressemblance de nom.
Les clearances hétérogènes sont majorées et annoncées, pas silencieusement perdues.
"""
import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

try:
    from .kicad_routage_vers_fusion import parse, nodes, field, net
except ImportError:
    from kicad_routage_vers_fusion import parse, nodes, field, net


def mm(value):
    match = re.fullmatch(r'\s*(\d+(?:\.\d*)?|\.\d+)\s*(mm|mil|inch|in)?\s*', str(value))
    if not match:
        raise ValueError(f'Dimension non reconnue : {value!r}')
    result = float(match[1]) * {None: 1, 'mm': 1, 'mil': .0254, 'inch': 25.4, 'in': 25.4}[match[2]]
    if not math.isfinite(result):
        raise ValueError('Dimension non finie.')
    return round(result, 9)


def copper_mapping(board, pcb):
    """L'ordre vient de l'empilement déclaré, pas du nombre de couches."""
    param = board.find('./designrules/param[@name="layerSetup"]')
    if param is None:
        raise ValueError('Empilement layerSetup absent du BRD.')
    setup = param.get('value', '')
    if not re.fullmatch(r'[\d()*+\[\]:\s]+', setup):
        raise ValueError('Syntaxe layerSetup non prise en charge.')
    # Les crochets et : décrivent des technologies complexes : ne pas les deviner.
    if any(c in setup for c in '[]:'):
        raise ValueError('Empilement spécial non pris en charge (vias traversants seulement).')
    eagle = [int(x) for x in re.findall(r'\d+', setup)]
    if len(eagle) not in (2, 4, 6) or len(set(eagle)) != len(eagle) or eagle[0] != 1:
        raise ValueError('Empilement attendu : 2, 4 ou 6 couches cuivre distinctes, Top en premier.')
    layers = [x[1] for x in nodes(pcb, 'layers')[0][1:] if isinstance(x, list) and x[1].endswith('.Cu')]
    def order(name):
        if name == 'F.Cu': return 0
        if name == 'B.Cu': return 100
        m = re.fullmatch(r'In(\d+)\.Cu', name)
        if not m: raise ValueError(f'Couche cuivre inconnue : {name}')
        return int(m[1])
    layers.sort(key=order)
    expected = ['F.Cu', *[f'In{i}.Cu' for i in range(1, len(eagle)-1)], 'B.Cu']
    if layers != expected:
        raise ValueError(f'Empilements incompatibles : BRD {eagle}, KiCad {layers}.')
    return dict(zip(layers, eagle))


def contact_mapping(board, pcb):
    source = {}
    for signal in board.findall('./signals/signal'):
        contacts = frozenset((c.get('element'), c.get('pad')) for c in signal.findall('contactref'))
        if not contacts and not len(signal):
            continue  # déclaration vide, aucun cuivre ni contact à apparier
        if not contacts or contacts in source:
            raise ValueError(f'Net sans contacts ou ambigu : {signal.get("name")}')
        source[contacts] = signal.get('name')
    target = {}
    seen = set()
    for fp in nodes(pcb, 'footprint'):
        ref = next((p[2] for p in nodes(fp, 'property') if p[1] == 'Reference'), '')
        for pad in nodes(fp, 'pad'):
            name = net(pad)
            if name:
                key = (ref, pad[1])
                if key in seen:
                    raise ValueError(f'Contact dupliqué : {ref}.{pad[1]}')
                seen.add(key)
                target.setdefault(name, set()).add(key)
    result = {}
    for name, contacts in target.items():
        key = frozenset(contacts)
        if key not in source:
            raise ValueError(f'Connectivité différente pour le net KiCad {name}.')
        result[source[key]] = name
    if set(result) != set(source.values()):
        raise ValueError('Certains nets Fusion ne sont pas retrouvés dans KiCad.')
    # Un net de piste sans contacts ne doit pas disparaître du contrôle.
    for kind in ('segment', 'arc', 'via', 'zone'):
        for obj in nodes(pcb, kind):
            if net(obj) and net(obj) not in target:
                raise ValueError(f'Net cuivre sans correspondance : {net(obj)}')
    return result


def modern_widths(board, class_names):
    """Résoudre les largeurs globales, par classe ou par nom exact de signal.

    Fusion choisit la première règle correspondante, priorité numérique croissante.
    Ne pas prendre le maximum des règles : cela changerait la sémantique Fusion.
    Les autres catégories sont inventoriées, jamais déclarées traduites par défaut.
    """
    root = board.find('./designrules/rules')
    inventory = {'rules': [], 'categories': [], 'other_nodes': [], 'complete': True}
    if root is None:
        return {}, inventory
    inventory['categories'] = [dict(c.attrib) for c in root.findall('rulecategory')]
    inventory['other_nodes'] = [ET.tostring(c, encoding='unicode') for c in root
                                if c.tag not in ('rule', 'rulecategory')]
    candidates = []
    net_classes = {s.get('name'):s.get('class', '0') for s in board.findall('./signals/signal')}
    # Des cibles net séparées permettent de comparer les priorités d'une règle
    # signal et d'une règle classe sans donner arbitrairement raison au signal.
    net_targets = {'@net:'+n:c for n,c in net_classes.items()}
    for index, rule in enumerate(root.findall('rule')):
        attrs = dict(rule.attrib)
        entry = {'index': index, 'source': attrs, 'xml': ET.tostring(rule, encoding='unicode'),
                 'status': 'untranslated', 'reason': 'Catégorie non traduite.',
                 'applied_classes': []}
        inventory['rules'].append(entry)
        if rule.get('enabled') == 'no':
            entry.update(status='disabled', reason='Règle désactivée dans Fusion.')
            continue
        if rule.get('type') != 'Minimum Copper Width':
            continue
        label = rule.get('name', str(index))
        def reject(reason):
            raise ValueError(f'Règle moderne de largeur non traduisible ({label}) : {reason}. '
                             'Aucun projet de routage généré.')
        allowed = {'type', 'name', 'builtin_ruleid', 'enabled', 'priority', 'onescope',
                   'value', 'preferredvalue', 'check_polywidth'}
        if set(attrs) - allowed or len(rule):
            reject('attribut ou contenu inconnu')
        if rule.get('enabled') != 'yes' or rule.get('check_polywidth') != 'no':
            reject('activation ou contrôle des polygones non pris en charge')
        categories = [c for c in inventory['categories'] if c.get('type') == rule.get('type')]
        if len(categories) > 1 or any(c.get('manualdrc') != 'yes' or c.get('livedrc') != 'yes'
                                      for c in categories):
            reject('activation DRC de la catégorie ambiguë ou désactivée')
        priority = rule.get('priority', '')
        if not re.fullmatch(r'\d+', priority):
            reject('priorité absente ou invalide')
        scope = rule.get('onescope', '')
        if not scope:
            targets = set(class_names) | set(net_targets)
        elif re.fullmatch(r'classes=\d+(?:,\d+)*', scope):
            targets = set(scope[8:].split(','))
            if not targets <= class_names.keys():
                reject('classe inconnue')
            targets |= {n for n,c in net_targets.items() if c in targets}
        elif scope.startswith('signal=') and scope[7:] in net_classes:
            targets = {'@net:'+scope[7:]}
        else:
            reject('seuls les scopes global, classes=... et signal=<nom exact existant> sont pris en charge')
        try:
            minimum, preferred = mm(rule.get('value')), mm(rule.get('preferredvalue'))
        except ValueError:
            reject('minimum ou valeur préférée invalide')
        if minimum <= 0 or preferred < minimum:
            reject('valeur préférée inférieure au minimum ou minimum nul')
        candidates.append((int(priority), targets, minimum, preferred, entry))
    # Une égalité de priorité sur des scopes recouvrants n'a pas de départage prouvé.
    for i, (priority, targets, *_rest) in enumerate(candidates):
        if any(priority == p and targets & t for p, t, *_ in candidates[i+1:]):
            raise ValueError('Règles modernes de largeur : priorités égales sur des classes communes.')
    resolved = {}
    for priority, targets, minimum, preferred, entry in sorted(candidates, key=lambda c: c[0]):
        applied = sorted(targets - resolved.keys())
        for cid in applied:
            resolved[cid] = {'minimum': minimum, 'preferred': preferred,
                             'rule_index': entry['index'], 'priority': priority}
        entry.update(status='translated' if applied else 'shadowed',
                     reason='Largeur des pistes : minimum DRC et préférence de routage.' if applied
                            else 'Masquée par une règle de priorité supérieure.',
                     applied_classes=[class_names[cid] for cid in applied if cid in class_names],
                     applied_nets=[cid[5:] for cid in applied if cid.startswith('@net:')])
    inventory['complete'] = not inventory['other_nodes'] and not any(
        r['status'] == 'untranslated' for r in inventory['rules'])
    return resolved, inventory


def transfer(brd_path, pcb_path, pro_path, output, name='carte', dru_path=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name):
        raise ValueError('Nom de livrable invalide.')
    output = Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Le dossier de sortie doit être vide (sources jamais écrasées).')
    if dru_path is None and Path(pro_path).with_suffix('.kicad_dru').exists():
        dru_path = Path(pro_path).with_suffix('.kicad_dru')
    if dru_path and Path(dru_path).read_text(encoding='utf-8-sig').strip() not in ('', '(version 1)'):
        raise ValueError('Règles personnalisées KiCad existantes : fusion automatique non validée. Conserver le .kicad_dru et examiner les conflits avant transfert.')
    board = ET.parse(brd_path).getroot().find('./drawing/board')
    if board is None: raise ValueError('BRD XML EAGLE attendu.')
    pcb = parse(Path(pcb_path).read_text(encoding='utf-8-sig'))
    layers = copper_mapping(board, pcb)
    mapping = contact_mapping(board, pcb)
    for via in nodes(pcb, 'via'):
        if via[1] in ('blind', 'micro') or field(via, 'layers') != ['F.Cu', 'B.Cu']:
            raise ValueError('Seuls les vias traversants sont admis.')
    for via in board.findall('./signals/signal/via'):
        if via.get('extent') != f'{list(layers.values())[0]}-{list(layers.values())[-1]}':
            raise ValueError('Le BRD contient un via non traversant ou une étendue inconnue.')
    project = json.loads(Path(pro_path).read_text(encoding='utf-8-sig'))
    if not isinstance(project, dict) or not isinstance(project.get('net_settings'), dict):
        raise ValueError('Projet .kicad_pro avec net_settings attendu.')
    before = copy.deepcopy(project)
    params = {p.get('name'): p.get('value') for p in board.findall('./designrules/param')}
    used = {'layerSetup'}
    def dim(key):
        if key not in params: raise ValueError(f'Règle Fusion requise absente : {key}')
        used.add(key)
        return mm(params[key])
    width, drill = dim('msWidth'), dim('msDrill')
    if min(width, drill) <= 0: raise ValueError('Minima de largeur/perçage nuls : choix explicite nécessaire.')
    keys = ('mdWireWire','mdWirePad','mdWireVia','mdPadPad','mdPadVia','mdViaVia','mdSmdPad','mdSmdVia','mdSmdSmd')
    clearances = {key: dim(key) for key in keys}
    base_clearance = max(clearances.values())
    warnings = []
    empty_signals = [s.get('name') for s in board.findall('./signals/signal') if not len(s)]
    if empty_signals:
        warnings.append('Déclarations de nets vides, sans contact ni cuivre : '+', '.join(empty_signals)+'. Conservées dans la source ; non matérialisées dans le PCB KiCad.')
    if len(set(clearances.values())) > 1:
        warnings.append('Clearances par type d’objet différentes : majoration conservatrice à leur maximum '+str(base_clearance)+' mm ; certaines distances seront plus grandes que dans Fusion.')
    rings = []
    for suffix in (('Outer', 'Inner') if len(layers) > 2 else ('Outer',)):
        ratio = dim('rvVia'+suffix)
        lo, hi = dim('rlMinVia'+suffix), dim('rlMaxVia'+suffix)
        if lo > hi: raise ValueError('Bornes d’anneau de via incohérentes.')
        rings.append((ratio, lo, hi))
    # Le minimum global KiCad couvre pads ET vias. Les minima Fusion doivent
    # rester propres à chaque objet/couche, sans durcir les autres objets.
    annular_minima = []
    for layer in layers:
        pad_suffix = 'Top' if layer == 'F.Cu' else 'Bottom' if layer == 'B.Cu' else 'Inner'
        via_suffix = 'Outer' if layer in ('F.Cu', 'B.Cu') else 'Inner'
        for kind, key in (('Pad', 'rlMinPad'+pad_suffix), ('Via', 'rlMinVia'+via_suffix)):
            annular_minima.append(dict(object=kind, layer=layer, minimum=dim(key)))
    classes = board.findall('./classes/class')
    if not classes or len({c.get('number') for c in classes}) != len(classes):
        raise ValueError('Classes Fusion absentes ou dupliquées.')
    class_names = {c.get('number'): 'Default' if c.get('number') == '0' else c.get('name') for c in classes}
    if '0' not in class_names or len(set(class_names.values())) != len(classes):
        raise ValueError('Noms de classes ambigus ou classe zéro absente.')
    modern, modern_report = modern_widths(board, class_names)
    if modern_report['rules']:
        warnings.append('Règles modernes : priorités croissantes, première règle applicable. '
                        'Les catégories non traduites restent à vérifier dans Fusion.')
    else:
        warnings.append('Aucune règle moderne présente dans la source : minimum/préférence distincts indisponibles ; largeurs historiques utilisées.')
    for entry in modern_report['rules']:
        if entry['status'] in ('untranslated', 'shadowed'):
            warnings.append(f"Règle moderne {entry['source'].get('name', entry['index'])} : {entry['reason']}")
    if modern_report['other_nodes']:
        warnings.append('Contenu moderne inconnu conservé dans le rapport, non traduit.')
    old_classes = {c['name']: c for c in project['net_settings'].get('classes', [])}
    template = old_classes.get('Default')
    if not template: raise ValueError('Classe Default manquante dans le projet KiCad.')
    generated, rows, dru = [], [], ['(version 1)', '# Généré depuis Fusion ; minima de classes et vias traversants.']
    # mdCopperDimension couvre aussi les trous mécaniques Fusion. Le minimum
    # global hole_clearance toucherait les vias/PTH : cibler seulement les NPTH,
    # sur le cuivre, y compris les objets sans net (tolérance physique).
    npth_clearance = dim('mdCopperDimension')
    for layer in layers:
        dru.extend([f'(rule "Fusion cuivre NPTH {layer}"',
                    f'  (layer "{layer}")',
                    '  (condition "A.Pad_Type == \'NPTH, mechanical\' || B.Pad_Type == \'NPTH, mechanical\'")',
                    f'  (constraint hole_clearance (min {npth_clearance}mm))',
                    f'  (constraint physical_hole_clearance (min {npth_clearance}mm)))'])
    for rule in annular_minima:
        condition = f"A.Type == '{rule['object']}'"
        if rule['object'] == 'Pad':
            condition += " && A.Pad_Type == 'Through-hole'"
        dru.extend([f'(rule "Fusion anneau {rule["object"]} {rule["layer"]}"',
                    f'  (layer "{rule["layer"]}")',
                    f'  (condition {json.dumps(condition)})',
                    f'  (constraint annular_width (min {rule["minimum"]}mm)))'])
    # Les clearances par paire EAGLE ne sont pas équivalentes au max des deux
    # classes KiCad. Majorant explicite : sûr, mais potentiellement moins routable.
    pair_values = {id_: [] for id_ in class_names}
    for cls in classes:
        for clearance in cls.findall('clearance'):
            other = clearance.get('class')
            if other not in pair_values: raise ValueError('Clearance vers une classe inconnue.')
            value = mm(clearance.get('value'))
            pair_values[cls.get('number')].append(value)
            pair_values[other].append(value)
    if any(pair_values.values()):
        warnings.append('Matrice de clearances entre classes majorée par classe ; pas une traduction exacte des paires.')
    for cls in classes:
        id_, cname = cls.get('number'), class_names[cls.get('number')]
        if not cname or any(c in cname for c in "'\"\\\r\n"):
            raise ValueError('Nom de classe incompatible avec les règles personnalisées.')
        cw, cd = max(width, mm(cls.get('width', '0'))), max(drill, mm(cls.get('drill', '0')))
        # Les anciennes classes ne doivent pas réimposer un maximum à la règle gagnante.
        width_minimum = modern[id_]['minimum'] if id_ in modern else cw
        cw = modern[id_]['preferred'] if id_ in modern else cw
        ring = max(max(lo, min(hi, ratio*cd)) for ratio, lo, hi in rings)
        diameter = round(cd+2*ring, 9)
        clearance = max([base_clearance, *pair_values[id_]])
        item = copy.deepcopy(old_classes.get(cname, template))
        item.update(name=cname, track_width=cw, clearance=clearance, via_diameter=diameter, via_drill=cd)
        generated.append(item)
        values = {k:item[k] for k in ('track_width','clearance','via_diameter','via_drill')}
        rows.append({'class':cname,'source':dict(cls.attrib),'before':old_classes.get(cname), 'after':values,
                     'width_policy': 'préférence de la règle moderne prioritaire' if id_ in modern
                                     else 'minimum historique Fusion utilisé comme largeur de routage',
                     'track_width_minimum': width_minimum, 'modern_width_rule': modern.get(id_),
                     'annular_width':ring})
        # La classe choisit une largeur ; seule une règle DRC impose son minimum.
        condition = json.dumps(f"A.NetClass == '{cname}'", ensure_ascii=False)
        dru.extend([f'(rule "Fusion {cname}"', f'  (condition {condition})',
                    f'  (constraint track_width (min {width_minimum}mm) (opt {cw}mm))',
                    f'  (constraint hole_size (min {cd}mm)))'])
    dru.extend(['(rule "Fusion vias traversants uniquement"', '  (condition "A.Type == \'Via\' && A.Via_Type != \'Through\'")', '  (constraint disallow via))'])
    assignments = []
    for signal in board.findall('./signals/signal'):
        if signal.get('name') not in mapping:
            continue
        cid = signal.get('class', '0')
        if cid not in class_names: raise ValueError('Affectation à une classe Fusion inconnue.')
        kn = mapping[signal.get('name')]
        # Les motifs KiCad sont des globs. Refuser un nom ambigu au lieu d'affecter
        # accidentellement plusieurs nets ; un futur échappement devra être validé.
        if any(c in kn for c in '*?[]'):
            raise ValueError(f'Nom de net contenant des jokers : {kn}')
        assignments.append({'pattern':kn, 'netclass':class_names[cid]})
    net_widths = {}
    for signal in board.findall('./signals/signal'):
        if signal.get('name') not in mapping:
            continue
        effective = modern.get('@net:'+signal.get('name'))
        if effective is None:
            continue
        kn = mapping[signal.get('name')]
        net_widths[kn] = {'minimum':effective['minimum'], 'preferred':effective['preferred']}
        base = modern.get(signal.get('class', '0'))
        if base and all(base[k] == effective[k] for k in ('minimum','preferred')):
            continue
        if any(c in kn for c in "'\"\\\r\n"):
            raise ValueError('Nom de net incompatible avec une règle exacte de largeur.')
        net_widths[kn] = {'minimum':effective['minimum'], 'preferred':effective['preferred']}
        # KiCad applique la dernière règle correspondante. La résolution Fusion
        # a déjà eu lieu : cette règle finale matérialise le résultat par net.
        condition = json.dumps(f"A.NetName == '{kn}'", ensure_ascii=False)
        dru.extend([f'(rule {json.dumps("Fusion net "+kn, ensure_ascii=False)}',
                    f'  (condition {condition})',
                    f"  (constraint track_width (min {effective['minimum']}mm) (opt {effective['preferred']}mm)))"])
    ns = project['net_settings']
    ns.update(classes=generated, netclass_assignments={}, netclass_patterns=assignments)
    rules = project.setdefault('board', {}).setdefault('design_settings', {}).setdefault('rules', {})
    # Le minimum global KiCad ne peut annuler une exception Fusion plus permissive.
    # Chaque classe reçoit ensuite son minimum propre dans le fichier .kicad_dru.
    selected_rules = {'min_track_width':min([r['track_width_minimum'] for r in rows]+[v['minimum'] for v in net_widths.values()]), 'min_through_hole_diameter':drill,
                      'min_clearance':base_clearance, 'min_copper_edge_clearance':dim('mdCopperDimension'),
                      'min_hole_to_hole':dim('mdDrill'),
                      'min_via_annular_width':min(r['minimum'] for r in annular_minima),
                      'min_via_diameter':min(c['via_diameter'] for c in generated)}
    rules.update(selected_rules)
    warnings.extend(['Largeur de routage = préférence moderne lorsqu’elle est traduite ; sinon minimum historique. Le minimum DRC est indiqué séparément.',
                     'Anneaux calculés pour les perçages de classe. Le cuivre existant est inchangé ; des vias redimensionnés nécessitent un nouveau contrôle.',
                     'Dans KRT : utiliser les valeurs des classes, désactiver Clearance ceiling et choisir Escalation off ; vérifier le résumé des dimensions.',
                     'Les règles non transférées sont inventoriées dans le rapport. Validation DRC et essai de routage requis.'])
    inherited = {c['name']:{k:v for k,v in c.items() if k in ('diff_pair_gap','diff_pair_width','diff_pair_via_gap','microvia_diameter','microvia_drill')} for c in generated}
    warnings.append('Paramètres différentiels conservés du projet KiCad, non traduits depuis Fusion : ne pas les considérer comme validés. Microvias interdits par la règle dédiée.')
    unhandled = {k:v for k,v in params.items() if k not in used}
    report = {'classes':rows, 'nets':len(mapping), 'layers':layers, 'net_mapping':mapping,
              'assignments':assignments, 'board_rules_before':before.get('board',{}).get('design_settings',{}).get('rules',{}),
              'board_rules_after':selected_rules, 'warnings':warnings, 'untranslated_rules':unhandled,
              'modern_rules':modern_report, 'net_widths':net_widths, 'empty_signals':empty_signals,
              'clearance_source':clearances, 'inherited_kicad_parameters':inherited,
              'annular_minima':annular_minima,
              'npth_copper_clearance':npth_clearance,
              'geometry_unchanged':True, 'drc_executed':False,
              'sources':{str(Path(p).resolve()):hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in (brd_path,pcb_path,pro_path)}}
    output.mkdir(parents=True, exist_ok=True)
    (output/(name+'.kicad_pcb')).write_bytes(Path(pcb_path).read_bytes())
    (output/(name+'.kicad_pro')).write_text(json.dumps(project, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    (output/(name+'.kicad_dru')).write_text('\n'.join(dru)+'\n', encoding='utf-8')
    (output/'rapport_regles.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    table = '\n'.join(f"| {r['class']} | {r['track_width_minimum']} / {r['after']['track_width']} | {r['after']['clearance']} | {r['after']['via_diameter']} / {r['after']['via_drill']} |" for r in rows)
    (output/'README.md').write_text(f'''# Transfert des règles Fusion → KiCad

Ouvrir {name}.kicad_pro depuis ce dossier extrait. Garder les trois fichiers KiCad ensemble.
La carte est copiée octet pour octet ; les règles du projet sont remplacées par les valeurs Fusion documentées.
Les bibliothèques externes et modèles 3D ne sont pas inclus ; les empreintes sont embarquées dans le PCB.

| Classe | Largeur min / routage mm | Clearance mm | Via / perçage mm |
| --- | ---: | ---: | ---: |
{table}

'''+'\n'.join('- '+w for w in warnings)+f'\n\n{len(unhandled)} paramètres Fusion non transférés : voir rapport_regles.json.\n'
        +'\nSélectionner une classe dans KRT et vérifier ses valeurs avant de router. La copie ne constitue pas une validation de fabrication.\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('brd', type=Path)
    parser.add_argument('pcb', type=Path)
    parser.add_argument('project', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--name', default='carte')
    parser.add_argument('--dru', type=Path)
    args = parser.parse_args()
    transfer(args.brd, args.pcb, args.project, args.output, args.name, args.dru)
