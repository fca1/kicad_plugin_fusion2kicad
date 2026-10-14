# Passerelle Fusion ↔ KiCad

Plugin pour KiCad 10 permettant d'importer une carte Fusion Electronics
BRD/FBRD, de préparer son routage avec KiCad Routing Tools, puis de
rapporter les pistes, vias et zones prises en charge vers Fusion.

## Prérequis

- KiCad 10 avec `kicad-cli` et son environnement Python (`pcbnew`, `wx`).
- Shapely 2 dans le Python utilisé par KiCad pour contrôler les jonctions.
- Le plugin KiCad Routing Tools complet pour lancer le routage KRT.
- Fusion Electronics pour exécuter manuellement le RUN ULP de retour.
- Un fichier source `.brd` ou `.fbrd` ; aucun complément Fusion n'est requis.

## Assemblage et installation

Depuis la racine du dépôt, avec Python :

```text
python tools/build_plugin.py dist
```

Choisir un dossier de destination neuf. Cette commande produit le dossier
`dist/passerelle_fusion` et une archive ZIP.

Copier `passerelle_fusion` dans le dossier des plugins de KiCad 10
(`3rdparty/plugins` dans le dossier utilisateur KiCad). Sauvegarder les cartes
ouvertes puis redémarrer l'éditeur PCB pour charger cette version.

## Utilisation

Ouvrir **Passerelle Fusion ↔ KiCad** depuis la barre d'outils ou les plugins externes.

1. **Source Fusion** : sélectionner le fichier `.brd` ou `.fbrd`. Sans source
   valide, les préparations sont grisées.
2. **Préparer vers KiCad — ouvrir KRT** : l'import crée une copie du projet et
   transfère les règles prises en charge. Le dialogue KRT permet de choisir le
   routage dans un nouvel éditeur PCB.
3. **Préparer depuis KiCad — générer le retour Fusion** : depuis l'éditeur de
   la carte routée, sélectionner la même source et préparer le livrable ULP.
4. **Copier la commande RUN** : coller puis exécuter la commande dans le PCB
   cible Fusion. L'ULP vérifie la cible, génère les commandes et applique le routage.

Le dossier des résultats, placé juste au-dessus de la zone **Résultat**, est
vide au départ. La sélection de la source propose son dossier parent ; une
destination choisie manuellement est conservée. La source et la destination
restent mémorisées pendant la session de cet éditeur, sans stockage persistant.
Un nouvel éditeur repart vide.

Le bouton **Copier le résultat** copie le texte de la zone en bas du dialogue.
Le bouton Détails a été supprimé ; les chemins des rapports sont indiqués dans
le résultat. L'accès direct à Fusion et le compagnon PasserelleKiCad ont été retirés.

Chaque opération conserve un instantané des sources et des rapports dans
un dossier distinct. Le retour remplace le routage des nets concernés.
Vérifier le remplissage des zones, les chevelus et le DRC dans Fusion.
Les composants, pastilles et nets logiques ne sont pas modifiés par le retour.

## Avertissements et limites

Les contacts cuivre sans jonction explicite sûre produisent un **avertissement
non bloquant** : l'ULP est généré et les contacts restent sans raccord supplémentaire.
Le détail figure dans `diagnostic_jonctions.json` et `rapport_conversion.json` ;
les contacts non résolus sont signalés brièvement dans le plugin. Les limites
détaillées du transfert restent conservées dans sa notice et les rapports.

Les raccords sous le minimum sont acceptés uniquement lorsque leur cuivre est
entièrement recouvert par le cuivre source et cible. Le normaliseur ne crée pas
de cuivre extérieur et ne reroute pas les pistes. Les minima modernes sont
résolus par signal ; les exceptions sont détaillées dans `covered_neckdowns`.
Des erreurs Copper Width peuvent subsister dans Fusion : contrôler le DRC.

Le vérificateur relit les commandes ULP, compare les objets livrés et contrôle
indépendamment la conservation des axes source par net, couche et largeur ; il
ne remplace pas la compilation ULP, l'application et le DRC dans Fusion.
Les erreurs qui empêchent une conversion valide restent bloquantes.
Le RUN revalide les anciennes classes et les formes de cuivre. Les règles
modernes ne sont pas accessibles par ULP : repréparer le retour si elles changent,
puis vérifier le DRC Fusion.

- Vias traversants uniquement ; arcs de pistes non pris en charge.
- Retour réel 4/6 couches non validé : mode préparation seulement.
- Zones pleines : au plus une par couche, contour droit sans trou,
  connexions directes et options de remplissage prises en charge uniquement.
- Les règles Fusion non traduites et les majorations conservatrices sont
  consignées dans les rapports. Fusion recalcule le remplissage avec ses règles.
- Les options de fabrication capping/filling des vias ne sont pas transférées.

## Organisation

- `plugin/passerelle_fusion` : interface KiCad et orchestration.
- `tools` : assemblage, conversion, règles et vérification du retour.

Ce dépôt distribue le code et sa documentation. Les cartes utilisateur,
exports de diagnostic et historiques de travail ne sont pas inclus.

## Licence

[MIT](LICENSE).

## Version affichée

Le titre affiche `Passerelle Fusion ↔ KiCad — r<numéro> · <hash>`.
Le numéro compte les commits accessibles depuis le commit source ; le hash
identifie la révision exacte. Chaque assemblage depuis un nouveau commit prend
sa nouvelle version ; une installation existante conserve sa version jusqu'à
mise à jour. Les modifications locales du code ajoutent la mention `modifié`.

Assembler depuis un clone Git : `python tools/build_plugin.py <dossier_neuf>`.
Pour une archive Git : `python tools/build_plugin.py <dossier_neuf> --version-repo <clone_source>`.
Le clone fournit l'historique du commit exporté. La version est embarquée dans
le paquet : Git n'est pas requis dans KiCad. Les compteurs sont propres à
l'historique de chaque dépôt ; deux historiques distincts peuvent donc donner
des numéros différents pour un même code.

Le bouton utilise le logo **F blanc sur orange / Ki blanc sur bleu**, reliés par des flèches. Le visuel source est conservé dans `plugin/passerelle_fusion/logo-source.png`, avec son icône transparente 32 pixels dans `icon.png`.
