# Source notes

Checked 2026-11-07 to 2026-11-09. Terms can change: ENTSO-E may revise its Terms of Use without
notice (clause 5.11).

| | ENTSO-E Transparency Platform | OMIE | E-REDES |
|---|---|---|---|
| Used for | generation by type, actual load, day-ahead price | Portugal day-ahead price | national consumption |
| Licence | ENTSO-E Terms of Use 2023. These items are **not** on its CC BY 4.0 open-data list (updated 2023-10-18), so reuse needs attribution (clause 3.1) and care not to harm the primary data owner | no explicit licence; legal notice asks to cite the source | CC BY 4.0 |
| Credit as | "Source: ENTSO-E Transparency Platform" | "Source: OMIE (www.omie.es)" | "Source: E-REDES Open Data Portal (CC BY 4.0)" |
| Access | free token (register, then email transparency@entsoe.eu) | plain file download, no key | open API, no key |
| Rate limit | 400 requests/min per token, temporary ban if exceeded | none stated | none stated |
| Resolution | load and generation hourly; price 15 minutes | hourly until 2025-09-30, 15 minutes from 2025-10-01 | 15 minutes |
| Lag | about 1 hour | published the day before | 2 to 3 days |
| History | long | daily files work from 2023 (earlier paths returned 404) | from 2023-01-01 |

Rejected: **REN Data Hub** (its terms forbid copying or extracting large parts of the site, and
its API returns chart data rather than a data feed) and the **REN monthly balance on dados.gov.pt**
(CC BY 4.0 but last updated 2022).

## Cross-checks

- **Price:** ENTSO-E and OMIE matched on 96 of 96 intervals on 2026-11-05 and 2026-11-06.
- **Demand:** ENTSO-E load (hourly, MW) against E-REDES (15 minutes, kWh, summed to the hour). The
  tolerance is set from history in step 6. These sources are not independent measurements (REN
  produces the load data that both republish), so the check finds publishing, unit and time-zone
  errors, not measurement errors.

## Odd things found in the real data

- OMIE switched from hourly to 15-minute prices on 2025-10-01 (contract 1.0.0 to 2.0.0).
- OMIE trading days follow Spanish time; 15-minute days have 92 to 100 periods.
- ENTSO-E series omit repeated values (curve type A03): a missing point means "same as the
  previous one". Staging expands them before any completeness check.
- ENTSO-E generation has separate series for generation and consumption of the same type (for
  example pumped storage).
- ENTSO-E responses carry a new document id and creation time on every request.
- E-REDES on the spring-forward day (2026-03-29) lists 4 extra rows with value 0 for local times
  that do not exist. On the fall-back day (2026-10-25) it returned 100 rows, 4 of them without a
  timestamp (labels 24:15 to 25:00), and none for 00:00 to 00:45 UTC.
- E-REDES 2026-11-01 (not a clock-change day) has 4 timestamps twice with different values and is
  missing its last 3 quarters. Recent days are published late and in parts (2026-11-04 had 1 of 96
  rows on 2026-11-09).
- All of this is handled by rejecting the rows, never repairing them: see data-model.md.
