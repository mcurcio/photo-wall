"""The one "not older" guard every upstream-versioned upsert shares (idempotent jobs rule 2, §6).

A row's upstream version is its manifest asset's `(updated_at, id)` (`UpstreamVersion`). An
observation is applied unless it is older than the stored one: a stored NULL version takes any
observation; a stored version takes only a set one that is not older, compared as the row value
(changed_at, id). Equal re-applies: the same upstream bytes, so the same facts (a prerelease flag
may still have changed). `app_releases` (029) and `node_release_observations` (065) both use it.
"""

from __future__ import annotations


def not_older(table: str) -> str:
    """The guard as a WHERE fragment over `table`'s `upstream_changed_at`/`upstream_asset_id`,
    with the observation as the named parameters `%(changed_at)s` and `%(asset_id)s`."""
    if not table.isidentifier():
        raise ValueError("invalid_table")
    return (f"({table}.upstream_changed_at IS NULL OR "
            "(%(changed_at)s::double precision IS NOT NULL AND "
            f"({table}.upstream_changed_at, {table}.upstream_asset_id) "
            "<= (%(changed_at)s::double precision, %(asset_id)s::bigint)))")
