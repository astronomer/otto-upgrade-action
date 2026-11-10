> ## Documentation Index
> Fetch the complete documentation index at: https://astronomer.io/docs/llms.txt
> Use this file to discover all available pages before exploring further.

# Astro Runtime release notes

> A summary of the latest Astro Runtime features and functionality. Astro Runtime is a Docker image built by Astronomer that provides a differentiated Apache Airflow experience and execution framework.

> [!TIP]
>
> [Subscribe to Astro Runtime release notes](https://astronomer.io/docs/astro/release-notes-subscribe.md) to receive updates via RSS, email, or Slack.

To see the most recent releases across all versions in release date order, see [Latest Astro Runtime releases](https://astronomer.io/docs/runtime/runtime-release-feed.md).

Astro Runtime is a Docker image built and published by Astronomer that extends the Apache Airflow project to provide a differentiated data orchestration experience. This document provides a summary of changes made to each available version of Astro Runtime.

To upgrade Astro Runtime, see either [Upgrade Astro Runtime](https://astronomer.io/docs/runtime/upgrade-astro-runtime.md) or [Upgrade Astro Private Cloud Runtime](https://astronomer.io/docs/runtime/manage-airflow-versions.md). For general product release notes, see [Astro Release Notes](https://astronomer.io/docs/astro/release-notes.md). If you have any questions or a bug to report, contact [Astronomer support](https://cloud.astronomer.io/open-support-request).

For release notes and provider package reference of Astro Runtime versions 10.x and older, see [Astro Runtime docs archive](https://github.com/astronomer/astronomer-docs-resources/tree/main/astro/runtime).

> [!NOTE]
>
> Because Astronomer has separate [maintenance life cycles](https://astronomer.io/docs/runtime/runtime-version-lifecycle-policy.md) for each major version of Astro Runtime, the same change can be introduced multiple times across major versions, resulting in multiple identical release notes. When a new major version releases, such as Astro Runtime 8.0.0, all changes from previously released versions are included in the new major version.
>
> If you're upgrading to receive a specific change, ensure the release note for the change appears either:
>
> * Within your target major version.
> * In any minor or patch version that was released before the first release (`X.0.0`) of your target major version. For example, a change in Astro Runtime 9.9.0, which released January 10 2024, is not guaranteed to appear in Runtime 10.0.0, which released December 8 2023, unless there is a release note for it in a subsequent Runtime 10 patch. However, a change in Astro Runtime 9.6.0, which released November 30 2023, is guaranteed to exist in Runtime 10.0.0 because 9.6.0 was released prior to 10.0.0.

**Astro Runtime 3.3-12**

November 05, 2026

* Airflow version: 3.3.2
* Python versions: 3.12 - 3.14 (default: 3.14)
* Runtime image: `astrocrpublic.azurecr.io/runtime:3.3-12`

### Additional improvements

* Upgrade to Airflow `3.3.2+astro.3`, which includes:
  * Cut the per-file cost of persisting Dag parse results ([#71621](https://github.com/apache/airflow/pull/71621))
  * Update waiting tasks to the latest Dag version ([#74592](https://github.com/apache/airflow/pull/74592))
  * Stop a stale callback stat from forcing constant Dag file reparsing ([#73433](https://github.com/apache/airflow/pull/73433))
  * Respect run on latest version when clearing a running Dag run ([#71425](https://github.com/apache/airflow/pull/71425))
  * Stop deadline callback triggers from moving between healthy triggerers, which ran the same callback more than once ([#72848](https://github.com/apache/airflow/pull/72848))
* Upgrade Python to 3.12.16, 3.13.18, and 3.14.10
* Upgrade `astronomer-kubernetes-executor` to `10.25.0+astro.1`
* Upgraded the minor and patch versions of some open-source provider packages. See [Astro Runtime 3.3-12 provider packages](https://www.astronomer.io/docs/astro/runtime-provider-reference#astro-runtime-3-3-12).

### Breaking changes

* `websockets` upgraded from 16.0 to 17.4 to fix [GHSA-6m5v-9gmr-5665](https://github.com/python-websockets/websockets/security/advisories/GHSA-6m5v-9gmr-5665). This is a major version upgrade. If your Dags or plugins use `websockets` directly, test them before you upgrade.

### Security fixes

* Fixed [GHSA-45c3-73f7-pv4m](https://github.com/advisories/GHSA-45c3-73f7-pv4m)
* Fixed [GHSA-6m5v-9gmr-5665](https://github.com/python-websockets/websockets/security/advisories/GHSA-6m5v-9gmr-5665)
* Fixed [GHSA-49g6-96jj-f7r9](https://github.com/pydantic/pydantic/security/advisories/GHSA-49g6-96jj-f7r9)
* Fixed [GHSA-jfv7-ghgv-gx4x](https://github.com/pydantic/pydantic/security/advisories/GHSA-jfv7-ghgv-gx4x)
* Fixed [GHSA-r49w-vh47-r4hf](https://github.com/pydantic/pydantic/security/advisories/GHSA-r49w-vh47-r4hf)
* Fixed [GHSA-955g-x97g-8mcp](https://github.com/pydantic/pydantic/security/advisories/GHSA-955g-x97g-8mcp)
* Fixed [GHSA-qpmf-fgh3-ff25](https://github.com/pydantic/pydantic/security/advisories/GHSA-qpmf-fgh3-ff25)
* Fixed [GHSA-c8m5-c952-6cm4](https://github.com/advisories/GHSA-c8m5-c952-6cm4)
* Fixed [GHSA-3r5h-5jcg-gmgg](https://github.com/advisories/GHSA-3r5h-5jcg-gmgg)
* Fixed [GHSA-h2v8-394m-p7vv](https://github.com/advisories/GHSA-h2v8-394m-p7vv)
* Fixed [GHSA-vp88-798v-h67f](https://github.com/advisories/GHSA-vp88-798v-h67f)
* Fixed [GHSA-w44r-93j7-qv28](https://github.com/advisories/GHSA-w44r-93j7-qv28)
* Fixed [RUSTSEC-2026-0258](https://osv.dev/vulnerability/RUSTSEC-2026-0258)
* Fixed [CVE-2026-114583](https://avd.aquasec.com/nvd/cve-2026-114583)
* Fixed [CVE-2024-3220](https://avd.aquasec.com/nvd/cve-2024-3220) (Python 3.12 and 3.13 images)
* Fixed [CVE-2026-109518](https://avd.aquasec.com/nvd/cve-2026-109518)
* Fixed [CVE-2026-111497](https://avd.aquasec.com/nvd/cve-2026-111497)
* Fixed [CVE-2026-115557](https://avd.aquasec.com/nvd/cve-2026-115557)
* Fixed [CVE-2026-112307](https://avd.aquasec.com/nvd/cve-2026-112307)
* Fixed [CVE-2026-12345](https://avd.aquasec.com/nvd/cve-2026-12345) (Python 3.13 image)

**Astro Runtime 3.3-11**

October 23, 2026

* Airflow version: 3.3.2
* Python versions: 3.12 - 3.14 (default: 3.14)
* Runtime image: `astrocrpublic.azurecr.io/runtime:3.3-11`

### Additional improvements

* New virtual environments created with `python -m venv` now get `pip` 26.2.2
* Upgrade `grpcio` to 1.84.1, whose bundled CA list no longer includes the roots affected by the eMudhra compromise

### Security fixes

* Fixed [GHSA-xcq7-p3pf-p88h](https://github.com/advisories/GHSA-xcq7-p3pf-p88h)
* Fixed [GHSA-r4j5-9xpp-3985](https://github.com/tornadoweb/tornado/security/advisories/GHSA-r4j5-9xpp-3985)

**Astro Runtime 3.3-10**

October 20, 2026

* Airflow version: 3.3.2
* Python versions: 3.12 - 3.14 (default: 3.14)
* Runtime image: `astrocrpublic.azurecr.io/runtime:3.3-10`

### Additional improvements

* Raise the `botocore` logger to `INFO`
* Distrust certificate authorities affected by the [eMudhra intermediate compromise](https://emudhra.we-have-a-problem.com/)
* Upgraded the minor and patch versions of some open-source provider packages. See [Astro Runtime 3.3-10 provider packages](https://www.astronomer.io/docs/astro/runtime-provider-reference#astro-runtime-3-3-10).

### Security fixes

* Fixed [CVE-2022-51020](https://avd.aquasec.com/nvd/cve-2022-51020)
* Fixed [CVE-2026-111750](https://avd.aquasec.com/nvd/cve-2026-111750)
* Fixed [GHSA-2g87-pp6c-29x5](https://github.com/advisories/GHSA-2g87-pp6c-29x5)
* Fixed [GHSA-494j-54xm-rwp3](https://github.com/advisories/GHSA-494j-54xm-rwp3)
* Fixed [GHSA-5wr8-gcqf-gqf3](https://github.com/advisories/GHSA-5wr8-gcqf-gqf3)
* Fixed [GHSA-6cjc-53gc-5rqm](https://github.com/advisories/GHSA-6cjc-53gc-5rqm)
* Fixed [GHSA-f9xw-jh8m-3mwq](https://github.com/advisories/GHSA-f9xw-jh8m-3mwq)

**Astro Runtime 3.2-14**

November 05, 2026

* Airflow version: 3.2.2
* Python versions: 3.12 - 3.14 (default: 3.13)
* Runtime image: `astrocrpublic.azurecr.io/runtime:3.2-14`

### Additional improvements

* Upgrade to Airflow `3.2.2+astro.8`, which includes:
  * Guard the XCom and Dag run pickle-removal migrations against NaN, Infinity, and NUL values ([#69064](https://github.com/apache/airflow/pull/69064), [#71926](https://github.com/apache/airflow/pull/71926))
  * Stop a stale callback stat from forcing constant Dag file reparsing ([#73433](https://github.com/apache/airflow/pull/73433))
  * Update waiting tasks to the latest Dag version ([#74592](https://github.com/apache/airflow/pull/74592))
  * Fix task runner failure on a duplicate task instance success update conflict ([#63355](https://github.com/apache/airflow/pull/63355))
* Upgrade Python to 3.12.16, 3.13.18, and 3.14.10
* Upgrade `astronomer-kubernetes-executor` to `10.25.0+astro.1`
* Upgraded the minor and patch versions of some open-source provider packages. See [Astro Runtime 3.2-14 provider packages](https://www.astronomer.io/docs/astro/runtime-provider-reference#astro-runtime-3-2-14).

### Breaking changes

* `websockets` upgraded from 16.0 to 17.4 to fix [GHSA-6m5v-9gmr-5665](https://github.com/python-websockets/websockets/security/advisories/GHSA-6m5v-9gmr-5665). This is a major version upgrade. If your Dags or plugins use `websockets` directly, test them before you upgrade.

### Security fixes

* Fixed [CVE-2024-3220](https://avd.aquasec.com/nvd/cve-2024-3220) (Python 3.12 and 3.13 images)
* Fixed [CVE-2026-97687](https://avd.aquasec.com/nvd/cve-2026-97687)
* Fixed [CVE-2026-97688](https://avd.aquasec.com/nvd/cve-2026-97688)
* Fixed [CVE-2026-97689](https://avd.aquasec.com/nvd/cve-2026-97689)
* Fixed [CVE-2026-109518](https://avd.aquasec.com/nvd/cve-2026-109518)
* Fixed [CVE-2026-111497](https://avd.aquasec.com/nvd/cve-2026-111497)
* Fixed [CVE-2026-114583](https://avd.aquasec.com/nvd/cve-2026-114583)
* Fixed [CVE-2026-114605](https://avd.aquasec.com/nvd/cve-2026-114605)
* Fixed [CVE-2026-114613](https://avd.aquasec.com/nvd/cve-2026-114613)
* Fixed [GHSA-3r5h-5jcg-gmgg](https://github.com/andialbrecht/sqlparse/security/advisories/GHSA-3r5h-5jcg-gmgg)
* Fixed [GHSA-45c3-73f7-pv4m](https://github.com/advisories/GHSA-45c3-73f7-pv4m)
* Fixed [GHSA-49g6-96jj-f7r9](https://github.com/pydantic/pydantic/security/advisories/GHSA-49g6-96jj-f7r9)
* Fixed [GHSA-6m5v-9gmr-5665](https://github.com/python-websockets/websockets/security/advisories/GHSA-6m5v-9gmr-5665)
* Fixed [GHSA-6v7p-g79w-8964](https://github.com/advisories/GHSA-6v7p-g79w-8964)
* Fixed [GHSA-955g-x97g-8mcp](https://github.com/pydantic/pydantic/security/advisories/GHSA-955g-x97g-8mcp)
* Fixed [GHSA-c8m5-c952-6cm4](https://github.com/advisories/GHSA-c8m5-c952-6cm4)
