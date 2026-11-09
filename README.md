# pt-energy-pipeline

Work in progress. Scheduled batch pipeline over open Portuguese electricity data
(ENTSO-E, OMIE, E-REDES) with data contracts, lineage, quality gates and a public dashboard.

The full README is written in step 9.

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in your own values
ruff check . && ruff format --check . && mypy && pytest
```

## Data sources and credits

- Source: ENTSO-E Transparency Platform (generation, load, day-ahead prices). Used under its Terms
  of Use; not on its CC BY 4.0 open-data list, so raw ENTSO-E files are not republished here.
- Source: OMIE (www.omie.es), day-ahead prices for Portugal.
- Source: E-REDES Open Data Portal, CC BY 4.0, national consumption.

Details and terms: [docs/sources.md](docs/sources.md). This project is not endorsed by any of them.

## Licence

Code: MIT. Data keeps its publishers' licences.
