"""DuckDB session example: the duckdb dependency exists ONLY in the sandbox.

See the sibling README.md for how the pieces fit. One-time setup: copy
.env.example to .env.local at the REPO ROOT and fill in your keys — the
justfile loads it automatically. Then, from this directory:

    just run            # Daytona (default)
    just run e2b        # E2B
"""

import argparse
import asyncio
from pathlib import Path

from pydantic import BaseModel

from remote import E2B, Daytona, RemoteSession, remote

ROOT = Path(__file__).parent
DB_PATH = "/tmp/trips.duckdb"


def _choose_backend() -> Daytona | E2B:
    # parse_known_args so the flag works on a direct run yet falls back to the
    # default when this module is imported (e.g. by `remote-box build`).
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--backend", choices=("daytona", "e2b"), default="daytona")
    args, _ = parser.parse_known_args()
    if args.backend == "e2b":
        return E2B(template_prefix="remote-box-duckdb")
    return Daytona(snapshot_name="remote-box-duckdb", create_timeout_seconds=300)


BACKEND = _choose_backend()


class LoadRequest(BaseModel):
    db_path: str


class LoadResult(BaseModel):
    rows: int


class Query(BaseModel):
    db_path: str
    sql: str


class QueryResult(BaseModel):
    rows: list[list[str]]


@remote(local_project_root=ROOT, backend=BACKEND)
async def load_trips(arg: LoadRequest) -> LoadResult:
    # duckdb is only installed in the sandbox image (see Dockerfile), so import
    # it inside the function — the module itself must stay importable locally.
    import duckdb

    con = duckdb.connect(arg.db_path)
    con.execute("CREATE OR REPLACE TABLE trips (city VARCHAR, distance_km DOUBLE)")
    con.execute(
        "INSERT INTO trips VALUES "
        "('SF', 12.3), ('SF', 3.2), ('NYC', 8.1), ('NYC', 1.5), ('LA', 25.0)"
    )
    rows = con.execute("SELECT count(*) FROM trips").fetchone()[0]
    con.close()
    return LoadResult(rows=rows)


@remote(local_project_root=ROOT, backend=BACKEND)
async def run_query(arg: Query) -> QueryResult:
    import duckdb

    con = duckdb.connect(arg.db_path, read_only=True)
    rows = con.execute(arg.sql).fetchall()
    con.close()
    return QueryResult(rows=[[str(value) for value in row] for row in rows])


async def main() -> None:
    try:
        import duckdb  # noqa: F401

        print("NOTE: duckdb IS installed locally — the demo still runs, but proves less.")
    except ModuleNotFoundError:
        print("duckdb is NOT installed locally — it exists only in the sandbox.")

    # One session = one sandbox: the .duckdb file written by the first call is
    # still there for the second.
    async with RemoteSession(backend=BACKEND, local_project_root=ROOT):
        loaded = await load_trips(LoadRequest(db_path=DB_PATH))
        print(f"loaded {loaded.rows} rows into {DB_PATH} inside the sandbox")

        result = await run_query(
            Query(
                db_path=DB_PATH,
                sql="SELECT city, round(sum(distance_km), 1) AS total "
                "FROM trips GROUP BY city ORDER BY total DESC",
            )
        )
        print("total distance by city (computed by DuckDB in the sandbox):")
        for city, total in result.rows:
            print(f"  {city}: {total} km")


if __name__ == "__main__":
    asyncio.run(main())
