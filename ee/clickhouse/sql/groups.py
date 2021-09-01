from ee.kafka_client.topics import KAFKA_GROUPS
from posthog.settings import CLICKHOUSE_CLUSTER, CLICKHOUSE_DATABASE

from .clickhouse import KAFKA_COLUMNS, REPLACING_MERGE_TREE, STORAGE_POLICY, kafka_engine, table_engine

GROUPS_TABLE = "groups"

DROP_PERSON_TABLE_SQL = f"DROP TABLE {GROUPS_TABLE} ON CLUSTER {CLICKHOUSE_CLUSTER}"

GROUPS_TABLE_BASE_SQL = """
CREATE TABLE {table_name} ON CLUSTER {cluster}
(
    id Int64,
    type_id Int64,
    created_at DateTime64,
    team_id Int64,
    properties VARCHAR
    {extra_fields}
) ENGINE = {engine}
"""

GROUPS_TABLE_SQL = (
    GROUPS_TABLE_BASE_SQL
    + """Order By (team_id, type, id)
{storage_policy}
"""
).format(
    table_name=GROUPS_TABLE,
    cluster=CLICKHOUSE_CLUSTER,
    engine=table_engine(GROUPS_TABLE, "_timestamp", REPLACING_MERGE_TREE),
    extra_fields=KAFKA_COLUMNS,
    storage_policy=STORAGE_POLICY,
)

KAFKA_GROUPS_TABLE_SQL = GROUPS_TABLE_BASE_SQL.format(
    table_name="kafka_" + GROUPS_TABLE, cluster=CLICKHOUSE_CLUSTER, engine=kafka_engine(KAFKA_GROUPS), extra_fields="",
)

# You must include the database here because of a bug in clickhouse
# related to https://github.com/ClickHouse/ClickHouse/issues/10471
GROUPS_TABLE_MV_SQL = f"""
CREATE MATERIALIZED VIEW {GROUPS_TABLE}_mv ON CLUSTER {CLICKHOUSE_CLUSTER}
TO {CLICKHOUSE_DATABASE}.{GROUPS_TABLE}
AS SELECT
id,
type,
created_at,
team_id,
properties,
_timestamp,
_offset
FROM {CLICKHOUSE_DATABASE}.kafka_{GROUPS_TABLE}
"""

# { ..., "group_0": 1325 }
# To join with events join using JSONExtractInt(events.properties, "$group_{type_id}")
