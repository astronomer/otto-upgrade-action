"""Print the env var names that `docker build --secret` specs read, one per line.

Reads the BUILD_SECRETS env var (one spec per line). A spec names an env var
when it says `env=NAME`, when it says `type=env` with `src=NAME`/`source=NAME`
(docker's other spelling of env-backed), or when it is a bare `id=NAME` with no
src/source/env (BuildKit falls back to the env var named by the id).

verify.sh and run-otto.sh use this list to strip those variables from every
subprocess that executes repository code outside the image build itself — the
input's contract is "exposed to your Dockerfile's build", not "exposed to the
Otto migration and the import-level verifier".
"""

import os
import sys


def env_names(specs: str) -> list[str]:
    names = []
    for line in specs.splitlines():
        line = line.strip()
        if not line:
            continue
        fields: dict[str, str] = {}
        for field in line.split(","):
            key, _, value = field.partition("=")
            fields[key] = value  # docker keeps the last occurrence; so do we
        if fields.get("env"):
            name = fields["env"]
        elif fields.get("type") == "env":
            # src/source name the env var here; a bare `type=env,id=NAME`
            # falls back to the id, like buildx itself.
            name = fields.get("src") or fields.get("source") or fields.get("id", "")
        elif "src" not in fields and "source" not in fields:
            name = fields.get("id", "")
        else:
            name = ""
        if name:
            names.append(name)
    return names


if __name__ == "__main__":
    sys.stdout.write("".join(f"{n}\n" for n in env_names(os.environ.get("BUILD_SECRETS", ""))))
