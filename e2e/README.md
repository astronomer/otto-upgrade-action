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

# Consumer-shape fixtures

`e2e/consumers/` holds one small project per real consumer's layout, each
verified at the default `parse` level by the `consumer-shapes` job, on every PR
and daily. The job asserts each shape's outcome, so a change made for one
consumer can't quietly break another, and the daily run catches drift in the
latest Astro CLI or the live Runtime release notes before a consumer's weekly
run does.

- `plain`: a plain project, run with `bump-blocking-pins`.
- `pytest-addopts-env`: project-wide pytest `addopts` that colour and reshape
  the integrity run's output, and a DAG that reads a value from a gitignored
  `.env` the workflow writes before the action. The `-regression` row adds a
  failure that only happens at the target Runtime; it must fail, not pass as
  pre-existing.
- `nested-standalone`: the project in `airflow/`, `dev.mode: standalone`, and
  `ENV` lines in the Dockerfile.
- `floating-base-secret`: a floating `-base` Runtime tag, a build secret
  (`required=false`), and `packages.txt`.
- `../astro-project` (row `no-astro-scaffolding`): no `.astro/` directory.
