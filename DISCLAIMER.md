# Disclaimer

**This project is unofficial. It is not affiliated with, endorsed by, sponsored by, or
approved by Euroleague Basketball, its member clubs, or any of its data partners.**

## Data origin

All data in this repository and in its published datasets originates from **Euroleague
Basketball** and is retrieved from publicly accessible HTTP endpoints operated by
Euroleague Basketball (`api-live.euroleague.net`, `live.euroleague.net`). These endpoints
are undocumented and carry no public developer programme, no published terms of use for
programmatic access, and no data licence.

Euroleague Basketball is credited as the origin of the underlying data on every export
this project produces.

## A second origin: the NBA

Some rows in `nba_player_season` originate from **NBA Properties, Inc.**, retrieved from
`stats.nba.com` through the third-party `nba_api` package. That endpoint is likewise
undocumented and carries no public licence for programmatic access. It is used only to
give a reference point for players who arrive in Europe with no record in this
competition, is fetched at build time rather than per request, and is always labelled as
NBA-derived so it is never mistaken for a EuroLeague figure. The takedown path below
applies to it equally.

## Purpose and scope

This project exists for **research and educational use**. It is a re-organisation of
publicly reachable information into a form suitable for statistical analysis.

## No warranty

The data is provided "as is", without warranty of any kind, express or implied. Figures
may be incomplete, delayed, or wrong. Upstream sources change without notice and without
versioning. Do not rely on this data for betting, for financial decisions, for
journalistic publication without independent verification, or for any purpose where being
wrong carries a cost.

Known data-quality limitations are documented in `docs/DATA_QUALITY.md` and every ETL run
publishes a report of the reconciliation checks that failed.

## Rights in the underlying data

This project claims **no ownership of the underlying data**. The MIT licence in `LICENSE`
covers **the code in this repository only** — the ETL, the schema, the derived metric
implementations, and the MCP server.

It does not and cannot grant you rights in the match data itself. Database rights,
sui generis database protection, and any contractual restrictions on the source remain
with Euroleague Basketball and its data partners.

**Commercial use of the data may require a licence from the official rights holders.**
If you intend to build a commercial product on it, seek your own legal advice and contact
Euroleague Basketball directly. Do not treat this project's existence as permission.

## How this project tries to be a good citizen

- The ETL is aggressively rate-limited and backs off on `429`, honouring `Retry-After`.
- Immutable data is cached permanently. A finished game is fetched exactly once, ever.
- The MCP server **never** contacts upstream at request time. It reads only from the
  local warehouse. No amount of user traffic to this project can generate load upstream.

## Takedown contact

If you represent Euroleague Basketball or a rights holder and want this project changed
or removed, we will act promptly and in good faith. There is no need for a formal legal
process to get our attention.

Contact: **open a GitHub issue titled `TAKEDOWN`** on this repository, or email the
address listed in the repository's `README.md` under "Contact".

We commit to responding within 7 days, and to taking down published datasets on request
while any disagreement is discussed.
