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

## Licence

Code: MIT. Data keeps its publishers' licences (credits added with the ingestion step).
