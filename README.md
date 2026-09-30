# Papírový bot MarketTrace

Kupuje po nákupech insiderů z vedení firem (zdroj: data MarketTrace ze SEC) a drží 90 dní.
Obchoduje **jen na papírovém účtu Alpaca**, adresa je v kódu natvrdo (`paper-api.alpaca.markets`).

- `config.json` – pravidla (velikost pozice, minimální nákup insidera, doba držení)
- `scripts/bot.py` – logika
- `state/` – otevřené pozice a historie (větev `state`)
- `index.html` – přehled výsledků
