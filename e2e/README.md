# E2E fixture project

A minimal Astro project the e2e workflow upgrades:

- `Dockerfile` pins an intentionally-behind Runtime tag so the resolver has a
  real jump to compute.
- `requirements.txt` pins two providers (and leaves one unpinned) so the
  provider path and the "unpinned is reported, never changed" rule both run.
- `dags/clean_etl_dag.py` is well-formed on modern Airflow.
- `dags/legacy_dag.py` uses pre-3.x conventions (`schedule_interval`, top-level
  `airflow.DAG`) — syntactically valid, but the kind of file the Otto migration
  rewrites and the import-level verifier flags at the target version.

The pinned versions drift behind the live feed over time; that's the point —
the e2e asserts the *mechanics* (a plan is produced, bumps applied, verification
passes), not a specific target tag.
