# Data contracts

One YAML contract per source dataset in `contracts/`. Each states owner, source and licence, grain,
primary key, UTC time zone, freshness expectation, typed fields with units and allowed ranges, and
a changelog. They are validated against `src/pt_energy/contracts_schema/contract.schema.json`.

| Contract | Status | Version | Notes |
|---|---|---|---|
| `omie_day_ahead_price` | active | 3.0.0 | 1.0.0 hourly, 2.0.0 15-minute (OMIE switched 2025-10-01), 3.0.0 time field renamed to `interval_start_utc` (breaking, with migration) |
| `eredes_national_consumption` | active | 1.1.0 | CC BY 4.0 |
| `entsoe_actual_generation` | active | 1.1.0 | hourly, per production type and direction |
| `entsoe_actual_load` | active | 1.1.0 | hourly |
| `entsoe_day_ahead_price` | active | 1.1.0 | 15-minute, matches OMIE |

Draft means version `0.x`: no consumers yet, so changes need only a version increase and a
changelog entry. The three ENTSO-E contracts were drafts until 2026-11-09, when real responses
confirmed them and they became `1.0.0`.

## What counts as a change

| Level | Changes |
|---|---|
| **Major (breaking)** | field removed or renamed; field type or unit changed; `nullable` false to true; grain, primary key, time zone or resolution changed |
| **Minor** | field added; range or allowed values changed; `nullable` true to false |
| **Patch** | owner, description, source, freshness, notes |

A breaking change must bump the major version, carry a migration in
`contracts/migrations/<name>_<from>_to_<to>.yaml`, and be marked `breaking: true` in the changelog.

## How it is enforced

`pt-energy contracts check` runs in CI and fails on:

- a contract that breaks the schema, has duplicate fields, a primary key that is not a field, or a
  status that does not match its version;
- a version bump smaller than the change (for example a removed field with a minor bump);
- a breaking change without a valid migration, or with a wrong `breaking` flag;
- a released snapshot that was edited (`contracts/history/LOCK.json` stores a sha256 per snapshot);
- a current contract that differs from its frozen snapshot, or a new version that was not frozen.

Releasing a version: edit `contracts/<name>.yaml`, bump `version`, add the changelog entry, then run
`pt-energy contracts freeze <name>`. Review the diff like code: the lock file makes any change to a
released snapshot visible.

The check cannot prove that a published snapshot was never rewritten together with its lock entry;
that is what code review is for.

## Not covered here

Validating data against a contract (types, ranges, nulls, uniqueness) is step 6, where checks are
generated from these files.
