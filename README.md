# mymc pour macOS

Un utilitaire pour manipuler les images de cartes mémoire PlayStation 2
(celles de PCSX2 : `Mcd001.ps2`, `Mcd002.ps2`…) : importer et exporter des
sauvegardes, les lister, les supprimer, en créer de nouvelles.

C'est un portage en **Python 3** de [mymc](https://www.csclub.uwaterloo.ca/~rridge/mymc/)
de Ross Ridge (domaine public), avec une interface **Qt** qui tourne
nativement sur macOS. Le code d'origine était en Python 2.7 avec une
interface wxPython et deux DLL Windows (`mymcsup.dll` pour la compression,
`mymcicon.dll` en Direct3D pour l'affichage des icônes 3D) : rien de tout
cela ne fonctionnait sur Mac.

![Copie d'écran](docs/screenshot.png)

## Ce qui a changé

| | mymc 2.7 (original) | cette version |
|---|---|---|
| Python | 2.7 (fin de vie en 2020) | 3.9 → 3.14 |
| Interface | wxPython | PySide6 / Qt 6, mode sombre, Retina |
| Icônes 3D | `mymcicon.dll`, Direct3D, Windows seulement | rendu logiciel NumPy, partout |
| Compression MAX Drive | `mymcsup.dll`, sinon 100× plus lent | Python pur, accéléré par NumPy |
| ECC | boucles Python | vectorisé NumPy (~50× plus rapide) |
| Ligne de commande | `optparse` | `argparse`, sous-commandes, `--help` par commande |
| Intégration macOS | — | bundle `.app`, double-clic depuis le Finder, glisser-déposer |

Le format des images produites est identique : chaque champ du superblock
correspond à celui d'une vraie carte PS2, et les images font exactement
8 650 752 octets comme celles de PCSX2.

## Installation

Il faut Python 3.9 ou plus récent. Sur un Mac avec Homebrew :

```bash
brew install python
```

Puis, depuis ce dossier :

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[gui]"
```

Les commandes `mymc` et `mymc-gui` sont alors dans `.venv/bin/`. Pour les
avoir partout, ajoute ce dossier à ton `PATH`, ou installe avec
[pipx](https://pipx.pypa.io/) :

```bash
pipx install ".[gui]"
```

**Sans interface graphique**, la bibliothèque et la ligne de commande
n'ont *aucune* dépendance :

```bash
pipx install .
```

NumPy reste vivement recommandé (`pip install ".[speed]"`) : il accélère
beaucoup le calcul des codes ECC. Sans lui, tout fonctionne quand même.

### Application macOS

Pour obtenir une icône dans le Dock, une barre de menus normale et les
associations de fichiers dans le Finder :

```bash
.venv/bin/python tools/make_app.py --output dist
```

Puis glisse `dist/mymc.app` dans `/Applications`. Double-cliquer sur un
`.ps2` l'ouvre ; double-cliquer sur une sauvegarde `.psu` ou `.max`
l'importe dans la carte ouverte.

Le bundle appelle l'interpréteur dans lequel mymc est installé : il n'est
donc pas autonome, ne le donne pas à quelqu'un d'autre tel quel. Pour une
application redistribuable, passe par PyInstaller ou py2app sur le même
point d'entrée (`mymc.cli:main_gui`).

## Interface graphique

```bash
mymc-gui                    # ou :  mymc Mcd001.ps2 gui
```

- liste triable des sauvegardes, avec titre, taille et date ;
- aperçu de l'**icône 3D animée** de la sauvegarde : clique-glisse pour la
  faire tourner, double-clic pour recadrer, clic droit pour l'éclairage,
  la caméra et l'animation ;
- import par glisser-déposer d'un `.psu`, `.max`, `.cbs`, `.sps` ou `.xps`
  sur la fenêtre ;
- export en `.psu` (EMS) ou `.max` (MAX Drive), avec barre de progression ;
- espace libre affiché en permanence dans la barre d'état.

Les titres japonais sont affichés tels quels ; *Affichage ▸ Transliterate
Japanese Titles* les convertit en caractères latins approchants
(`ＤＡＴＡ` → `DATA`, `【あ】` → `[あ]`).

## Ligne de commande

Le principe : `mymc IMAGE COMMANDE [options]`.

```bash
# créer une carte de 8 Mo
mymc Mcd001.ps2 format

# voir ce qu'elle contient, comme dans le navigateur de la PS2
mymc Mcd001.ps2 dir

# importer des sauvegardes (le format est détecté tout seul)
mymc Mcd001.ps2 import ~/Downloads/*.psu ~/Downloads/*.max

# exporter, avec un nom de fichier descriptif
mymc Mcd001.ps2 export -l BASLUS-20678SAVE
# → « SLUS-20678 UNLIMITED SAGA SYSTEMDATA (9AA6AB3E).psu »

# exporter au format MAX Drive
mymc Mcd001.ps2 export -m BASLUS-20678SAVE

# supprimer une sauvegarde, vérifier le système de fichiers
mymc Mcd001.ps2 delete BASLUS-20678SAVE
mymc Mcd001.ps2 check
```

Commandes disponibles : `dir`, `ls`, `add`, `extract`, `mkdir`, `remove`,
`import`, `export`, `delete`, `set`, `clear`, `rename`, `df`, `check`,
`format`, `gui`. Chacune a son aide :

```bash
mymc Mcd001.ps2 export --help
```

### Formats de sauvegarde

| Format | Extension | Lecture | Écriture |
|---|---|:---:|:---:|
| EMS | `.psu` | oui | oui |
| MAX Drive | `.max` | oui | oui |
| Code Breaker | `.cbs` | oui | — |
| SharkPort / X-Port | `.sps`, `.xps` | oui | — |
| nPort | `.npo` | — | — |

## Utilisation comme bibliothèque

```python
from mymc import ps2mc, ps2save

with open("Mcd001.ps2", "r+b") as f:
    with ps2mc.ps2mc(f) as mc:
        print(mc.get_free_space() // 1024, "Ko libres")

        with open("save.psu", "rb") as g:
            mc.import_save_file(ps2save.load_save_file(g), ignore_existing=True)

        sf = mc.export_save_file("/BASLUS-20678SAVE")
        with open("copie.max", "wb") as g:
            sf.save_max_drive(g)
```

Rendre l'icône 3D d'une sauvegarde en PNG, sans interface graphique :

```python
from mymc import ps2icon, render

icon = ps2icon.parse_icon(open("list.icn", "rb").read())
render.render_to_png(icon, "icone.png", size=256, angle=0.6)
```

## Précautions

- **Ne modifie pas une image pendant que PCSX2 l'utilise.** L'émulateur
  garde la carte en mémoire et la réécrira par-dessus tes modifications,
  voire la corrompra. Ferme PCSX2 d'abord.
- Fais une sauvegarde de tes images avant les opérations d'écriture. Le
  code d'origine se décrivait comme de qualité « alpha » ; ce portage est
  couvert par des tests, mais la prudence reste de mise avec des données
  auxquelles tu tiens.
- La liste des blocs défectueux est ignorée, comme dans l'original. Sans
  effet sur les images créées par PCSX2 ou par mymc, qui n'en ont pas.

## Développement

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest          # 79 tests
```

Les tests couvrent les allers-retours de compression LZARI, la correction
d'erreurs ECC (comparée bit à bit à l'algorithme d'origine), le système de
fichiers, les quatre formats de sauvegarde, le rendu des icônes et la
ligne de commande de bout en bout.

Organisation :

```
src/mymc/
    ps2mc.py        système de fichiers de la carte
    ps2mc_dir.py    entrées de répertoire
    ps2mc_ecc.py    codes de Hamming (ECC)
    ps2save.py      formats .psu / .max / .cbs / .sps
    lzari.py        codec LZARI (compression MAX Drive)
    ps2icon.py      lecture du format d'icône 3D .icn
    render.py       rastériseur logiciel pour les icônes
    cli.py          ligne de commande
    gui/            interface Qt
```

### Notes de portage

Trois points ont demandé de l'attention :

- **`bytes` et `str`.** Le contenu des fichiers est en `bytes` partout ;
  les noms d'entrées de répertoire sont exposés en `str` via `latin-1`,
  qui fait correspondre un à un les octets 0–255 aux 256 premiers points
  de code : l'aller-retour reste donc exact, même pour un nom non ASCII.
- **La division.** L'arithmétique du codeur LZARI dépend d'une division
  entière tronquée ; les 16 divisions du module d'origine ont été
  reprises une par une en division entière (`//`).
- **L'ECC.** Le portage a été comparé à une transcription littérale de
  l'algorithme Python 2 sur 500 motifs d'erreur aléatoires : aucune
  divergence. Les erreurs d'un bit sont corrigées, celles de deux bits
  détectées.

## Licence

Domaine public, comme le mymc d'origine. Voir `LICENSE.txt`.

mymc a été écrit par Ross Ridge. L'algorithme LZARI est de
Haruhiko Okumura.
