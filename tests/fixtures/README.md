# Test fixtures

No network is used in tests. Fixtures are recorded or built as follows.

| Folder | Origin | Licence / credit |
|---|---|---|
| `omie/` | Real files downloaded 2026-11-09 (`2025-09-30` hourly, `2026-11-05` 15-minute). | Source: OMIE (www.omie.es) |
| `eredes/` | Real API responses downloaded 2026-11-09 for 2026-11-03 (normal), 2026-03-29 (spring forward) and 2026-10-25 (fall back). | Source: E-REDES Open Data Portal, CC BY 4.0 |
| `entsoe/` | **Synthetic.** Built by hand with the same XML structure as real responses (A03 series with omitted repeated points, separate in/out series, overlapping price periods). Values are invented, except that the price series reuses the OMIE values above, which matched ENTSO-E exactly when checked on 2026-11-05 and 2026-11-06. | ENTSO-E raw data is not on its CC BY 4.0 open-data list, so real payloads are kept out of this public repo. |
