"""Verifie le JavaScript embarque dans les pages Python.

Le JS de `web.py` et `report.py` vit dans des chaines Python : aucun test ne
l'execute, une faute de frappe ne se voit qu'a l'ouverture du navigateur.

    python scripts/check_js.py

Necessite node. Sortie 0 si tout va bien.

DEUX niveaux de verification, et le second existe pour une raison precise.

`node --check` ne lit que la SYNTAXE. Il a laisse passer un
`SEUIL_PROFITABLE is not defined` : la refonte de l'interface avait emporte la
declaration JS de cette constante, la constante Python du meme nom existait
toujours, et rien ne signalait l'absence. Ouvrir un contrat depuis l'historique
levait une ReferenceError et la page restait muette.

On EXECUTE donc aussi les fonctions de rendu, sous un DOM bouchonne, avec des
donnees representatives. Un nom manquant dans un corps de fonction ne se voit
qu'a l'appel : il faut appeler.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

#: Un DOM minimal : assez pour que le code de haut niveau de la page
#: s'execute sans navigateur. Chaque bouchon renvoie un objet accueillant
#: plutot que `undefined`, sans quoi on testerait le bouchon et pas la page.
BOUCHON = """
const __noeud = () => ({
  addEventListener() {}, removeEventListener() {},
  value: 'industrial', textContent: '', innerHTML: '', checked: false,
  className: '', disabled: false, dataset: {},
  selectedOptions: [{ textContent: 'defaut' }],
  scrollIntoView() {}, closest() { return null }, click() {},
  classList: { toggle() {}, add() {}, remove() {} },
});
// On ne declare PAS `$` : la page le fait elle-meme
// (`const $ = s => document.querySelector(s)`). Le redeclarer masquerait
// justement le code qu'on veut exercer.
const document = {
  querySelector: __noeud, querySelectorAll: () => [],
  addEventListener() {}, createElement: __noeud,
};
const window = { addEventListener() {} };
const fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
const confirm = () => false;
const alert = () => {};
const prompt = () => null;
process.on('unhandledRejection', () => {});
"""

#: Un plan complet, tel que le serveur l'envoie. Les valeurs importent peu ;
#: ce qui compte est que TOUS les champs lus par le rendu soient presents,
#: et que les cas limites (null) soient couverts.
PLAN = """
const __plan = {
  collection: 'The Arabesque Collection', rarity: 'Mil-Spec Grade',
  rarity_target: 'Restricted', currency: 'EUR',
  cost: 4.83, net: 6.17, profit: 1.34, roi: 0.28, profitability: 1.28,
  win_probability: 1.0, outcomes_count: 3, stdev: 0.05, avg_float: 0.13,
  listings_examined: 300, float_slack: 0.02,
  worst_profit: 1.28, best_profit: 1.40, all_profitable: true,
  downgrade_profit: -0.5, exit_loss: null, exit_loss_ratio: null,
  price_drop_tolerance: null, replis: 0,
  buy_market: 'CSFloat', sell_market: 'Steam',
  cost_alt: 8.14, profitability_alt: 0.76,
  cost_deep: 4.92, profitability_deep: 1.25, fragile: false,
  float_subi_ok: true, order_budget: 5.14, order_discount: 0.31,
  inputs: [{ name: 'Sawed-Off | Lunar Wyrm (Minimal Wear)', float: 0.1246,
             price: 0.50, url: 'https://csfloat.com/item/1' }],
  outcomes: [{ name: 'AUG | Lapis Lazuli (Minimal Wear)', probability: 0.3333,
               float: 0.1443, net: 6.23 }],
};
"""

#: Les appels a exercer. Chacun doit produire du HTML sans lever, et sans
#: laisser d'`undefined` ni de `NaN` dans la sortie -- un trou d'affichage est
#: un bug, meme silencieux.
APPELS = """
const __cas = [
  ['carte, plan frais', () => carte(__plan, 'abc', false)],
  ['carte, plan archive', () => carte(__plan, 'abc', true)],
  ['carte, contrat fragile',
   () => carte({ ...__plan, fragile: true, cost_deep: null,
                 profitability_deep: null }, 'abc', false)],
  ['carte, repli CSFloat', () => carte({ ...__plan, replis: 12 }, 'abc', false)],
  ['carte, prix Steam absent',
   () => carte({ ...__plan, cost_alt: null, profitability_alt: null },
               'abc', false)],
  ['carte, voie Steam fermee',
   () => carte({ ...__plan, float_subi_ok: false }, 'abc', false)],
  ['carte, sortie perdante',
   () => carte({ ...__plan, all_profitable: false, worst_profit: -0.4 },
               'abc', false)],
];

let __ko = 0;
for (const [nom, fn] of __cas) {
  let html;
  try {
    html = fn();
  } catch (e) {
    console.log('  [KO] ' + nom + ' -> ' + e.constructor.name + ' : ' + e.message);
    __ko++;
    continue;
  }
  const trou = /undefined|NaN/.exec(html);
  if (trou) {
    console.log('  [KO] ' + nom + ' -> valeur vide dans le rendu (' +
                trou[0] + ')');
    __ko++;
  } else {
    console.log('  [OK] ' + nom + ' (' + html.length + ' octets)');
  }
}
if (__ko) { process.exit(1); }
"""


def extraire(source: str, nom: str) -> list[tuple[str, str]]:
    return [(f"{nom}#{i}", m) for i, m in
            enumerate(re.findall(r"<script>(.*?)</script>", source, re.S), 1)]


def verifie_syntaxe(tmp: Path, blocs: list[tuple[str, str]]) -> int:
    echecs = 0
    for nom, js in blocs:
        f = tmp / "bloc.js"
        f.write_text(js, encoding="utf-8")
        r = subprocess.run(["node", "--check", str(f)],
                           capture_output=True, text=True)
        if r.returncode:
            echecs += 1
            print(f"[KO] {nom}\n{r.stderr.strip()}", file=sys.stderr)
        else:
            print(f"[OK] {nom} ({len(js)} octets)")
    return echecs


def verifie_rendu(tmp: Path, js: str) -> int:
    """Execute les fonctions de rendu de la page principale.

    C'est ce niveau qui attrape un nom manquant : `node --check` ne le voit
    pas, et un corps de fonction ne revele ses references qu'a l'appel.
    """
    f = tmp / "rendu.js"
    f.write_text(BOUCHON + js + PLAN + APPELS, encoding="utf-8")
    r = subprocess.run(["node", str(f)], capture_output=True, text=True)
    print("\nRendu de la carte :")
    print(r.stdout.rstrip() or "  (aucune sortie)")
    if r.returncode:
        if r.stderr.strip():
            print(r.stderr.strip(), file=sys.stderr)
        return 1
    return 0


def main() -> int:
    from tradeupfinder.report import _JS
    from tradeupfinder.web import PAGE

    blocs = extraire(PAGE, "web.PAGE") + [("report._JS", _JS)]
    if not blocs:
        print("aucun bloc <script> trouve", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as dossier:
        tmp = Path(dossier)
        echecs = verifie_syntaxe(tmp, blocs)
        if echecs:
            return 1
        echecs += verifie_rendu(tmp, blocs[0][1])
    return 1 if echecs else 0


if __name__ == "__main__":
    raise SystemExit(main())
