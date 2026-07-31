"""build_secret_env_names.py extracts the env vars docker --secret specs read."""

import pytest
from build_secret_env_names import env_names


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        ("id=netrc,env=NETRC_CONTENT", ["NETRC_CONTENT"]),
        ("type=env,id=netrc,src=NETRC_CONTENT", ["NETRC_CONTENT"]),
        ("id=netrc,source=NETRC_CONTENT,type=env", ["NETRC_CONTENT"]),
        # Explicit type=env with no src/source: buildx falls back to the id.
        ("type=env,id=PRIVATE_TOKEN", ["PRIVATE_TOKEN"]),
        # Bare id: BuildKit falls back to the env var named by the id.
        ("id=NETRC_CONTENT", ["NETRC_CONTENT"]),
        # File-backed specs read no env var.
        ("id=netrc,src=/run/netrc", []),
        ("id=netrc,src=.netrc", []),
        # env= wins over src= (docker resolves env-backed first).
        ("id=netrc,env=NETRC_CONTENT,src=ignored", ["NETRC_CONTENT"]),
        # Multiple specs, blank and whitespace-only lines skipped.
        (
            "id=a,env=VAR_A\n\n   \nid=b,src=/f\ntype=env,id=c,src=VAR_C\n",
            ["VAR_A", "VAR_C"],
        ),
        ("", []),
    ],
)
def test_env_names(specs, expected):
    assert env_names(specs) == expected
