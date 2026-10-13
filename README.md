# Passerelle Fusion ↔ KiCad

Plugin pour KiCad 10 permettant d'importer une carte Fusion Electronics
BRD/FBRD, de préparer son routage avec KiCad Routing Tools, puis de
rapporter les pistes, vias et zones prises en charge vers Fusion.

## Prérequis

- KiCad 10 avec `kicad-cli` et son environnement Python (`pcbnew`, `wx`).
- Shapely 2 dans le Python utilisé par KiCad pour contrôler les jonctions.
- Le plugin KiCad Routing Tools complet pour lancer le routage KRT.
- Fusion Electronics pour appliquer le retour ; le compagnon Fusion fourni
  est nécessaire pour lire le PCB actif sans sélectionner de fichier source.

## Assemblage et installation

Depuis la racine du dépôt, avec Python :

```text
python tools/build_plugin.py dist
```

Choisir un dossier de destination neuf. Cette commande produit le dossier
`dist/passerelle_fusion`, une archive ZIP et le compagnon `dist/PasserelleKiCad`.

Copier `passerelle_fusion` dans le dossier des plugins de KiCad 10
(`3rdparty/plugins` dans le dossier utilisateur KiCad). Sauvegarder les cartes
ouvertes puis redémarrer l'éditeur PCB pour charger cette version.

Pour utiliser le PCB actif de Fusion, ajouter le dossier `PasserelleKiCad`
comme complément Python dans le gestionnaire Scripts et compléments de Fusion,
puis le démarrer. Le compagnon doit s'exécuter dans Fusion.

## Utilisation

Ouvrir **Passerelle Fusion ↔ KiCad** depuis la barre d'outils ou les plugins externes.

1. **Préparer la carte et ouvrir le dialogue KRT** : sélectionner le BRD/FBRD
   source, ou utiliser le PCB actif via le compagnon Fusion. L'import crée
   un projet KiCad et transfère les règles prises en charge. Le dialogue KRT
   permet de choisir les paramètres de routage.
2. **Préparer le retour des pistes et vias vers Fusion** : depuis la carte
   KiCad routée, sélectionner le BRD/FBRD source et préparer le résultat.
   Copier la commande RUN affichée dans le PCB cible ouvert dans Fusion.
   L'ULP vérifie la cible, génère les commandes et applique le routage.

Chaque opération conserve un instantané des sources et des rapports dans
un dossier distinct. Le retour remplace le routage des nets concernés.
Vérifier le remplissage des zones, les chevelus et le DRC dans Fusion.
Les composants, pastilles et nets logiques ne sont pas modifiés par le retour.

## Avertissements et limites

Les contacts cuivre sans jonction explicite sûre produisent un **avertissement
non bloquant** : l'ULP est généré et les contacts restent sans raccord supplémentaire.
Le détail figure dans `diagnostic_jonctions.json` et `rapport_conversion.json` ;
les avertissements sont affichés dans le plugin et conservés dans sa notice.

Le vérificateur relit les commandes ULP et compare les objets livrés ; il
ne remplace pas la compilation ULP, l'application et le DRC dans Fusion.
Les erreurs qui empêchent une conversion valide restent bloquantes.

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
- `fusion_addin/PasserelleKiCad` : compagnon exécuté dans Fusion.

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
