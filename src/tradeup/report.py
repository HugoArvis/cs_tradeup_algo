"""Rapport HTML d'un plan d'achat : lisible, cliquable, autonome.

Pourquoi une page plutot qu'un tableau de terminal : les dix annonces d'un
panier sont des objets precis et perissables. Les retrouver a la main sur le
site, un par un, par leur float, prend de longues minutes -- pendant lesquelles
certaines partent, ce qui change la moyenne de float et donc le resultat du
contrat. Un lien direct par ligne supprime ce delai.

Le fichier produit est autonome (CSS inline, aucune ressource externe) et
s'ouvre hors ligne.
"""

from __future__ import annotations

import html
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path

from .plan import Plan

CSFLOAT_SEARCH = "https://csfloat.com/search?market_hash_name="

_CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1b1f24; --muted: #5b6673;
  --line: #e2e6eb; --pos: #0f7a3d; --neg: #b3261e; --warn: #8a5a00;
  --warn-bg: #fff6e0; --accent: #1a56b0;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #14171b; --card: #1c2126; --ink: #e8ecf1; --muted: #9aa5b1;
    --line: #2b3239; --pos: #4ec27e; --neg: #ff6b5e; --warn: #f0b400;
    --warn-bg: #2e2609; --accent: #6ba5ff;
  }
}
:root[data-theme="dark"] {
  --bg: #14171b; --card: #1c2126; --ink: #e8ecf1; --muted: #9aa5b1;
  --line: #2b3239; --pos: #4ec27e; --neg: #ff6b5e; --warn: #f0b400;
  --warn-bg: #2e2609; --accent: #6ba5ff;
}
:root[data-theme="light"] {
  --bg: #f6f7f9; --card: #ffffff; --ink: #1b1f24; --muted: #5b6673;
  --line: #e2e6eb; --pos: #0f7a3d; --neg: #b3261e; --warn: #8a5a00;
  --warn-bg: #fff6e0; --accent: #1a56b0;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 24px 16px 64px; background: var(--bg); color: var(--ink);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 880px; margin: 0 auto; }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
.card {
  background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px; margin-bottom: 16px;
}
.kpis { display: flex; flex-wrap: wrap; gap: 20px; }
.kpi { flex: 1 1 130px; }
.kpi .lab { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: .04em; }
.kpi .val { font-size: 24px; font-weight: 650; font-variant-numeric: tabular-nums; }
.pos { color: var(--pos); } .neg { color: var(--neg); }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin: 0 0 12px; }
.scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line);
  white-space: nowrap; }
th { font-size: 12px; color: var(--muted); font-weight: 600; }
td.num, th.num { text-align: right; }
tfoot td { font-weight: 650; border-bottom: none; }
a.buy {
  display: inline-block; padding: 4px 12px; border-radius: 6px;
  background: var(--accent); color: #fff; text-decoration: none; font-size: 13px;
  font-weight: 600;
}
a.buy:hover { filter: brightness(1.1); }
.warn {
  background: var(--warn-bg); border-left: 3px solid var(--warn);
  padding: 12px 14px; border-radius: 0 8px 8px 0; margin-bottom: 12px;
  font-size: 14px;
}
.warn b { color: var(--warn); }
.muted { color: var(--muted); font-size: 13px; }
.stale { display: none; }
"""

_JS = """
(function () {
  var born = new Date(document.body.dataset.generated);
  function tick() {
    var min = Math.floor((Date.now() - born.getTime()) / 60000);
    var el = document.getElementById('age');
    if (!el) return;
    el.textContent = min < 1 ? "a l'instant" : "il y a " + min + " min";
    // Au-dela de 15 minutes, un panier d'annonces n'est plus fiable.
    var stale = document.getElementById('stale');
    if (stale) stale.style.display = min >= 15 ? 'block' : 'none';
  }
  tick();
  setInterval(tick, 30000);
})();
"""


def _money(v: float) -> str:
    return f"{v:,.2f}".replace(",", " ")


def render(plan: Plan, *, currency: str = "USD") -> str:
    """Genere la page HTML complete d'un plan."""
    r = plan.result
    now = datetime.now()
    cls = "pos" if r.ev_profit >= 0 else "neg"

    lignes = []
    for opt in sorted(plan.options, key=lambda o: (o.skin.name, o.float_value)):
        lien = (
            f'<a class="buy" href="{html.escape(opt.url)}" target="_blank" '
            f'rel="noopener">Acheter</a>'
            if opt.url
            else '<span class="muted">annonce non identifiee</span>'
        )
        lignes.append(
            "<tr>"
            f"<td>{html.escape(opt.name)}</td>"
            f'<td class="num">{opt.float_value:.4f}</td>'
            f'<td class="num">{_money(opt.unit_cost)}</td>'
            f"<td>{lien}</td>"
            "</tr>"
        )

    sorties = []
    for o in r.outcomes:
        url = CSFLOAT_SEARCH + urllib.parse.quote(o.name)
        sorties.append(
            "<tr>"
            f'<td><a href="{html.escape(url)}" target="_blank" rel="noopener">'
            f"{html.escape(o.name)}</a></td>"
            f'<td class="num">{o.probability:.1%}</td>'
            f'<td class="num">{o.float_value:.4f}</td>'
            f'<td class="num">{_money(o.net_value)}</td>'
            "</tr>"
        )

    avertissements = [
        '<div class="warn" id="stale" style="display:none"><b>Panier perime.</b> '
        "Cette page a plus de 15 minutes. Les annonces listees ont pu etre "
        "vendues : relancez la commande avant d'acheter.</div>"
    ]
    tol = plan.price_drop_tolerance
    if tol is not None:
        avertissements.append(
            f'<div class="warn"><b>Blocage de 7 jours.</b> Les objets achetes sur '
            f"CSFloat arrivent par echange entre joueurs : ils ne seront "
            f"utilisables dans un contrat que dans 7 jours. Le prix de sortie "
            f"peut baisser de <b>{tol:.1%}</b> d'ici la avant que l'operation "
            f"ne devienne perdante.</div>"
        )
    if plan.downgrade_profit is not None:
        avertissements.append(
            f'<div class="warn"><b>Tolerance de float : '
            f"{plan.float_slack:.4f}</b> sur la somme des dix entrees. Si une "
            f"annonce part et que la remplacante depasse cette marge, la sortie "
            f"perd un palier d'usure : <span class=\"neg\">"
            f"{plan.downgrade_profit:+.2f}</span> au lieu de "
            f'<span class="pos">{r.ev_profit:+.2f}</span>.</div>'
        )

    return f"""<style>{_CSS}</style>
<body data-generated="{now.isoformat()}">
<div class="wrap">
  <h1>{html.escape(plan.collection.name)}</h1>
  <div class="sub">
    Plan genere <span id="age">a l'instant</span>
    &middot; {now.strftime('%d/%m/%Y %H:%M')}
    &middot; montants en {html.escape(currency)}
    &middot; {plan.listings_examined} annonces examinees
  </div>

  <div class="card kpis">
    <div class="kpi"><div class="lab">Cout des 10 entrees</div>
      <div class="val">{_money(r.cost)}</div></div>
    <div class="kpi"><div class="lab">Revente nette</div>
      <div class="val">{_money(r.ev_net)}</div></div>
    <div class="kpi"><div class="lab">Profit</div>
      <div class="val {cls}">{r.ev_profit:+.2f}</div></div>
    <div class="kpi"><div class="lab">Rendement</div>
      <div class="val {cls}">{r.roi:+.1%}</div></div>
  </div>

  {''.join(avertissements)}

  <div class="card">
    <h2>A acheter &mdash; 10 annonces precises</h2>
    <div class="scroll"><table>
      <thead><tr><th>Objet</th><th class="num">Float</th>
        <th class="num">Prix</th><th></th></tr></thead>
      <tbody>{''.join(lignes)}</tbody>
      <tfoot><tr><td>Moyenne de float : {r.avg_input_float:.4f}</td>
        <td class="num"></td><td class="num">{_money(r.cost)}</td><td></td></tr></tfoot>
    </table></div>
    <p class="muted">Achetez les dix le plus groupe possible : le compteur de
    7 jours demarre a la reception de chaque objet, c'est le dernier qui
    commande.</p>
  </div>

  <div class="card">
    <h2>Sortie</h2>
    <div class="scroll"><table>
      <thead><tr><th>Skin</th><th class="num">Probabilite</th>
        <th class="num">Float</th><th class="num">Net</th></tr></thead>
      <tbody>{''.join(sorties)}</tbody>
    </table></div>
  </div>
</div>
<script>{_JS}</script>
</body>"""


def write_and_open(
    plan: Plan, path: Path | str, *, currency: str = "USD", open_browser: bool = True
) -> Path:
    """Ecrit le rapport et l'ouvre dans le navigateur par defaut."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(plan, currency=currency), encoding="utf-8")
    if open_browser:
        webbrowser.open(p.resolve().as_uri())
    return p
