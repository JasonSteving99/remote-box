"""Backend selection shared by the remote_* examples.

Choose with a flag: `--backend daytona` (the default) or `--backend e2b`.
Parsed with parse_known_args at import time so it composes with anything else
on the command line, and quietly falls back to the default when these modules
are imported by something other than a direct script run (e.g.
`remote-box build examples/`, whose argv carries no --backend flag).
"""

import argparse

from remote import E2B, Daytona


def _choose() -> Daytona | E2B:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--backend", choices=("daytona", "e2b"), default="daytona")
    args, _ = parser.parse_known_args()
    match args.backend:
        case "e2b":
            return E2B(template_prefix="remote-box-example")
        case _:
            # First sandbox from a freshly built snapshot can exceed the default
            # 120s while the runner pulls the image.
            return Daytona(snapshot_name="remote-box-example", create_timeout_seconds=300)


BACKEND = _choose()
