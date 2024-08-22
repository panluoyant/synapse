#
# This file is licensed under the Affero General Public License (AGPL) version 3.
#
# Copyright 2019-2022 The Matrix.org Foundation C.I.C.
# Copyright (C) 2023 New Vector, Ltd
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# See the GNU Affero General Public License for more details:
# <https://www.gnu.org/licenses/agpl-3.0.html>.
#
# Originally licensed under the Apache License, Version 2.0:
# <http://www.apache.org/licenses/LICENSE-2.0>.
#
# [This file includes modifications made by New Vector Limited]
#
#

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple, cast

import attr

from synapse.api.constants import EventContentFields, Membership, RelationTypes
from synapse.api.room_versions import KNOWN_ROOM_VERSIONS
from synapse.events import EventBase, make_event_from_dict
from synapse.storage._base import SQLBaseStore, db_to_json, make_in_list_sql_clause
from synapse.storage.database import (
    DatabasePool,
    LoggingDatabaseConnection,
    LoggingTransaction,
    make_tuple_comparison_clause,
)
from synapse.storage.databases.main.events import (
    SLIDING_SYNC_RELEVANT_STATE_SET,
    PersistEventsStore,
    SlidingSyncMembershipInfo,
    SlidingSyncMembershipSnapshotSharedInsertValues,
    SlidingSyncStateInsertValues,
)
from synapse.storage.databases.main.state_deltas import StateDeltasStore
from synapse.storage.databases.main.stream import StreamWorkerStore
from synapse.storage.types import Cursor
from synapse.types import JsonDict, RoomStreamToken, StateMap, StrCollection
from synapse.types.handlers import SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
from synapse.types.state import StateFilter

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)


_REPLACE_STREAM_ORDERING_SQL_COMMANDS = (
    # there should be no leftover rows without a stream_ordering2, but just in case...
    "UPDATE events SET stream_ordering2 = stream_ordering WHERE stream_ordering2 IS NULL",
    # now we can drop the rule and switch the columns
    "DROP RULE populate_stream_ordering2 ON events",
    "ALTER TABLE events DROP COLUMN stream_ordering",
    "ALTER TABLE events RENAME COLUMN stream_ordering2 TO stream_ordering",
    # ... and finally, rename the indexes into place for consistency with sqlite
    "ALTER INDEX event_contains_url_index2 RENAME TO event_contains_url_index",
    "ALTER INDEX events_order_room2 RENAME TO events_order_room",
    "ALTER INDEX events_room_stream2 RENAME TO events_room_stream",
    "ALTER INDEX events_ts2 RENAME TO events_ts",
)


class _BackgroundUpdates:
    EVENT_ORIGIN_SERVER_TS_NAME = "event_origin_server_ts"
    EVENT_FIELDS_SENDER_URL_UPDATE_NAME = "event_fields_sender_url"
    DELETE_SOFT_FAILED_EXTREMITIES = "delete_soft_failed_extremities"
    POPULATE_STREAM_ORDERING2 = "populate_stream_ordering2"
    INDEX_STREAM_ORDERING2 = "index_stream_ordering2"
    INDEX_STREAM_ORDERING2_CONTAINS_URL = "index_stream_ordering2_contains_url"
    INDEX_STREAM_ORDERING2_ROOM_ORDER = "index_stream_ordering2_room_order"
    INDEX_STREAM_ORDERING2_ROOM_STREAM = "index_stream_ordering2_room_stream"
    INDEX_STREAM_ORDERING2_TS = "index_stream_ordering2_ts"
    REPLACE_STREAM_ORDERING_COLUMN = "replace_stream_ordering_column"

    EVENT_EDGES_DROP_INVALID_ROWS = "event_edges_drop_invalid_rows"
    EVENT_EDGES_REPLACE_INDEX = "event_edges_replace_index"

    EVENTS_POPULATE_STATE_KEY_REJECTIONS = "events_populate_state_key_rejections"

    EVENTS_JUMP_TO_DATE_INDEX = "events_jump_to_date_index"

    SLIDING_SYNC_JOINED_ROOMS_BACKFILL = "sliding_sync_joined_rooms_backfill"
    SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BACKFILL = (
        "sliding_sync_membership_snapshots_backfill"
    )


@attr.s(slots=True, frozen=True, auto_attribs=True)
class _CalculateChainCover:
    """Return value for _calculate_chain_cover_txn."""

    # The last room_id/depth/stream processed.
    room_id: str
    depth: int
    stream: int

    # Number of rows processed
    processed_count: int

    # Map from room_id to last depth/stream processed for each room that we have
    # processed all events for (i.e. the rooms we can flip the
    # `has_auth_chain_index` for)
    finished_room_map: Dict[str, Tuple[int, int]]


class EventsBackgroundUpdatesStore(StreamWorkerStore, StateDeltasStore, SQLBaseStore):
    def __init__(
        self,
        database: DatabasePool,
        db_conn: LoggingDatabaseConnection,
        hs: "HomeServer",
    ):
        super().__init__(database, db_conn, hs)

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME,
            self._background_reindex_origin_server_ts,
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME,
            self._background_reindex_fields_sender,
        )

        self.db_pool.updates.register_background_index_update(
            "event_contains_url_index",
            index_name="event_contains_url_index",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering"],
            where_clause="contains_url = true AND outlier = false",
        )

        # an event_id index on event_search is useful for the purge_history
        # api. Plus it means we get to enforce some integrity with a UNIQUE
        # clause
        self.db_pool.updates.register_background_index_update(
            "event_search_event_id_idx",
            index_name="event_search_event_id_idx",
            table="event_search",
            columns=["event_id"],
            unique=True,
            psql_only=True,
        )

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.DELETE_SOFT_FAILED_EXTREMITIES,
            self._cleanup_extremities_bg_update,
        )

        self.db_pool.updates.register_background_update_handler(
            "redactions_received_ts", self._redactions_received_ts
        )

        # This index gets deleted in `event_fix_redactions_bytes` update
        self.db_pool.updates.register_background_index_update(
            "event_fix_redactions_bytes_create_index",
            index_name="redactions_censored_redacts",
            table="redactions",
            columns=["redacts"],
            where_clause="have_censored",
        )

        self.db_pool.updates.register_background_update_handler(
            "event_fix_redactions_bytes", self._event_fix_redactions_bytes
        )

        self.db_pool.updates.register_background_update_handler(
            "event_store_labels", self._event_store_labels
        )

        self.db_pool.updates.register_background_index_update(
            "redactions_have_censored_ts_idx",
            index_name="redactions_have_censored_ts",
            table="redactions",
            columns=["received_ts"],
            where_clause="NOT have_censored",
        )

        self.db_pool.updates.register_background_index_update(
            "users_have_local_media",
            index_name="users_have_local_media",
            table="local_media_repository",
            columns=["user_id", "created_ts"],
        )

        self.db_pool.updates.register_background_update_handler(
            "rejected_events_metadata",
            self._rejected_events_metadata,
        )

        self.db_pool.updates.register_background_update_handler(
            "chain_cover",
            self._chain_cover_index,
        )

        self.db_pool.updates.register_background_update_handler(
            "purged_chain_cover",
            self._purged_chain_cover_index,
        )

        self.db_pool.updates.register_background_update_handler(
            "event_arbitrary_relations",
            self._event_arbitrary_relations,
        )

        ################################################################################

        # bg updates for replacing stream_ordering with a BIGINT
        # (these only run on postgres.)

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.POPULATE_STREAM_ORDERING2,
            self._background_populate_stream_ordering2,
        )
        # CREATE UNIQUE INDEX events_stream_ordering ON events(stream_ordering2);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2,
            index_name="events_stream_ordering",
            table="events",
            columns=["stream_ordering2"],
            unique=True,
        )
        # CREATE INDEX event_contains_url_index ON events(room_id, topological_ordering, stream_ordering) WHERE contains_url = true AND outlier = false;
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_CONTAINS_URL,
            index_name="event_contains_url_index2",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering2"],
            where_clause="contains_url = true AND outlier = false",
        )
        # CREATE INDEX events_order_room ON events(room_id, topological_ordering, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_ROOM_ORDER,
            index_name="events_order_room2",
            table="events",
            columns=["room_id", "topological_ordering", "stream_ordering2"],
        )
        # CREATE INDEX events_room_stream ON events(room_id, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_ROOM_STREAM,
            index_name="events_room_stream2",
            table="events",
            columns=["room_id", "stream_ordering2"],
        )
        # CREATE INDEX events_ts ON events(origin_server_ts, stream_ordering);
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.INDEX_STREAM_ORDERING2_TS,
            index_name="events_ts2",
            table="events",
            columns=["origin_server_ts", "stream_ordering2"],
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.REPLACE_STREAM_ORDERING_COLUMN,
            self._background_replace_stream_ordering_column,
        )

        ################################################################################

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS,
            self._background_drop_invalid_event_edges_rows,
        )

        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.EVENT_EDGES_REPLACE_INDEX,
            index_name="event_edges_event_id_prev_event_id_idx",
            table="event_edges",
            columns=["event_id", "prev_event_id"],
            unique=True,
            # the old index which just covered event_id is now redundant.
            replaces_index="ev_edges_id",
        )

        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS,
            self._background_events_populate_state_key_rejections,
        )

        # Add an index that would be useful for jumping to date using
        # get_event_id_for_timestamp.
        self.db_pool.updates.register_background_index_update(
            _BackgroundUpdates.EVENTS_JUMP_TO_DATE_INDEX,
            index_name="events_jump_to_date_idx",
            table="events",
            columns=["room_id", "origin_server_ts"],
            where_clause="NOT outlier",
        )

        # Backfill the sliding sync tables
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BACKFILL,
            self._sliding_sync_joined_rooms_backfill,
        )
        self.db_pool.updates.register_background_update_handler(
            _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BACKFILL,
            self._sliding_sync_membership_snapshots_backfill,
        )

    async def _background_reindex_fields_sender(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        def reindex_txn(txn: LoggingTransaction) -> int:
            sql = (
                "SELECT stream_ordering, event_id, json FROM events"
                " INNER JOIN event_json USING (event_id)"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]

            update_rows = []
            for row in rows:
                try:
                    event_id = row[1]
                    event_json = db_to_json(row[2])
                    sender = event_json["sender"]
                    content = event_json["content"]

                    contains_url = "url" in content
                    if contains_url:
                        contains_url &= isinstance(content["url"], str)
                except (KeyError, AttributeError):
                    # If the event is missing a necessary field then
                    # skip over it.
                    continue

                update_rows.append((sender, contains_url, event_id))

            sql = "UPDATE events SET sender = ?, contains_url = ? WHERE event_id = ?"

            txn.execute_batch(sql, update_rows)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows),
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, progress
            )

            return len(rows)

        result = await self.db_pool.runInteraction(
            _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME, reindex_txn
        )

        if not result:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_FIELDS_SENDER_URL_UPDATE_NAME
            )

        return result

    async def _background_reindex_origin_server_ts(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        def reindex_search_txn(txn: LoggingTransaction) -> int:
            sql = (
                "SELECT stream_ordering, event_id FROM events"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]
            event_ids = [row[1] for row in rows]

            rows_to_update = []

            chunks = [event_ids[i : i + 100] for i in range(0, len(event_ids), 100)]
            for chunk in chunks:
                ev_rows = cast(
                    List[Tuple[str, str]],
                    self.db_pool.simple_select_many_txn(
                        txn,
                        table="event_json",
                        column="event_id",
                        iterable=chunk,
                        retcols=["event_id", "json"],
                        keyvalues={},
                    ),
                )

                for event_id, json in ev_rows:
                    event_json = db_to_json(json)
                    try:
                        origin_server_ts = event_json["origin_server_ts"]
                    except (KeyError, AttributeError):
                        # If the event is missing a necessary field then
                        # skip over it.
                        continue

                    rows_to_update.append((origin_server_ts, event_id))

            sql = "UPDATE events SET origin_server_ts = ? WHERE event_id = ?"

            txn.execute_batch(sql, rows_to_update)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows_to_update),
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME, progress
            )

            return len(rows_to_update)

        result = await self.db_pool.runInteraction(
            _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME, reindex_search_txn
        )

        if not result:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_ORIGIN_SERVER_TS_NAME
            )

        return result

    async def _cleanup_extremities_bg_update(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Background update to clean out extremities that should have been
        deleted previously.

        Mainly used to deal with the aftermath of https://github.com/matrix-org/synapse/issues/5269.
        """

        # This works by first copying all existing forward extremities into the
        # `_extremities_to_check` table at start up, and then checking each
        # event in that table whether we have any descendants that are not
        # soft-failed/rejected. If that is the case then we delete that event
        # from the forward extremities table.
        #
        # For efficiency, we do this in batches by recursively pulling out all
        # descendants of a batch until we find the non soft-failed/rejected
        # events, i.e. the set of descendants whose chain of prev events back
        # to the batch of extremities are all soft-failed or rejected.
        # Typically, we won't find any such events as extremities will rarely
        # have any descendants, but if they do then we should delete those
        # extremities.

        def _cleanup_extremities_bg_update_txn(txn: LoggingTransaction) -> int:
            # The set of extremity event IDs that we're checking this round
            original_set = set()

            # A dict[str, Set[str]] of event ID to their prev events.
            graph: Dict[str, Set[str]] = {}

            # The set of descendants of the original set that are not rejected
            # nor soft-failed. Ancestors of these events should be removed
            # from the forward extremities table.
            non_rejected_leaves = set()

            # Set of event IDs that have been soft failed, and for which we
            # should check if they have descendants which haven't been soft
            # failed.
            soft_failed_events_to_lookup = set()

            # First, we get `batch_size` events from the table, pulling out
            # their successor events, if any, and the successor events'
            # rejection status.
            txn.execute(
                """SELECT prev_event_id, event_id, internal_metadata,
                    rejections.event_id IS NOT NULL, events.outlier
                FROM (
                    SELECT event_id AS prev_event_id
                    FROM _extremities_to_check
                    LIMIT ?
                ) AS f
                LEFT JOIN event_edges USING (prev_event_id)
                LEFT JOIN events USING (event_id)
                LEFT JOIN event_json USING (event_id)
                LEFT JOIN rejections USING (event_id)
                """,
                (batch_size,),
            )

            for prev_event_id, event_id, metadata, rejected, outlier in txn:
                original_set.add(prev_event_id)

                if not event_id or outlier:
                    # Common case where the forward extremity doesn't have any
                    # descendants.
                    continue

                graph.setdefault(event_id, set()).add(prev_event_id)

                soft_failed = False
                if metadata:
                    soft_failed = db_to_json(metadata).get("soft_failed")

                if soft_failed or rejected:
                    soft_failed_events_to_lookup.add(event_id)
                else:
                    non_rejected_leaves.add(event_id)

            # Now we recursively check all the soft-failed descendants we
            # found above in the same way, until we have nothing left to
            # check.
            while soft_failed_events_to_lookup:
                # We only want to do 100 at a time, so we split given list
                # into two.
                batch = list(soft_failed_events_to_lookup)
                to_check, to_defer = batch[:100], batch[100:]
                soft_failed_events_to_lookup = set(to_defer)

                sql = """SELECT prev_event_id, event_id, internal_metadata,
                    rejections.event_id IS NOT NULL
                    FROM event_edges
                    INNER JOIN events USING (event_id)
                    INNER JOIN event_json USING (event_id)
                    LEFT JOIN rejections USING (event_id)
                    WHERE
                        NOT events.outlier
                        AND
                """
                clause, args = make_in_list_sql_clause(
                    self.database_engine, "prev_event_id", to_check
                )
                txn.execute(sql + clause, list(args))

                for prev_event_id, event_id, metadata, rejected in txn:
                    if event_id in graph:
                        # Already handled this event previously, but we still
                        # want to record the edge.
                        graph[event_id].add(prev_event_id)
                        continue

                    graph[event_id] = {prev_event_id}

                    soft_failed = db_to_json(metadata).get("soft_failed")
                    if soft_failed or rejected:
                        soft_failed_events_to_lookup.add(event_id)
                    else:
                        non_rejected_leaves.add(event_id)

            # We have a set of non-soft-failed descendants, so we recurse up
            # the graph to find all ancestors and add them to the set of event
            # IDs that we can delete from forward extremities table.
            to_delete = set()
            while non_rejected_leaves:
                event_id = non_rejected_leaves.pop()
                prev_event_ids = graph.get(event_id, set())
                non_rejected_leaves.update(prev_event_ids)
                to_delete.update(prev_event_ids)

            to_delete.intersection_update(original_set)

            deleted = self.db_pool.simple_delete_many_txn(
                txn=txn,
                table="event_forward_extremities",
                column="event_id",
                values=to_delete,
                keyvalues={},
            )

            logger.info(
                "Deleted %d forward extremities of %d checked, to clean up matrix-org/synapse#5269",
                deleted,
                len(original_set),
            )

            if deleted:
                # We now need to invalidate the caches of these rooms
                rows = cast(
                    List[Tuple[str]],
                    self.db_pool.simple_select_many_txn(
                        txn,
                        table="events",
                        column="event_id",
                        iterable=to_delete,
                        keyvalues={},
                        retcols=("room_id",),
                    ),
                )
                room_ids = {row[0] for row in rows}
                for room_id in room_ids:
                    txn.call_after(
                        self.get_latest_event_ids_in_room.invalidate, (room_id,)  # type: ignore[attr-defined]
                    )

            self.db_pool.simple_delete_many_txn(
                txn=txn,
                table="_extremities_to_check",
                column="event_id",
                values=original_set,
                keyvalues={},
            )

            return len(original_set)

        num_handled = await self.db_pool.runInteraction(
            "_cleanup_extremities_bg_update", _cleanup_extremities_bg_update_txn
        )

        if not num_handled:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.DELETE_SOFT_FAILED_EXTREMITIES
            )

            def _drop_table_txn(txn: LoggingTransaction) -> None:
                txn.execute("DROP TABLE _extremities_to_check")

            await self.db_pool.runInteraction(
                "_cleanup_extremities_bg_update_drop_table", _drop_table_txn
            )

        return num_handled

    async def _redactions_received_ts(self, progress: JsonDict, batch_size: int) -> int:
        """Handles filling out the `received_ts` column in redactions."""
        last_event_id = progress.get("last_event_id", "")

        def _redactions_received_ts_txn(txn: LoggingTransaction) -> int:
            # Fetch the set of event IDs that we want to update
            sql = """
                SELECT event_id FROM redactions
                WHERE event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
            """

            txn.execute(sql, (last_event_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            (upper_event_id,) = rows[-1]

            # Update the redactions with the received_ts.
            #
            # Note: Not all events have an associated received_ts, so we
            # fallback to using origin_server_ts. If we for some reason don't
            # have an origin_server_ts, lets just use the current timestamp.
            #
            # We don't want to leave it null, as then we'll never try and
            # censor those redactions.
            sql = """
                UPDATE redactions
                SET received_ts = (
                    SELECT COALESCE(received_ts, origin_server_ts, ?) FROM events
                    WHERE events.event_id = redactions.event_id
                )
                WHERE ? <= event_id AND event_id <= ?
            """

            txn.execute(sql, (self._clock.time_msec(), last_event_id, upper_event_id))

            self.db_pool.updates._background_update_progress_txn(
                txn, "redactions_received_ts", {"last_event_id": upper_event_id}
            )

            return len(rows)

        count = await self.db_pool.runInteraction(
            "_redactions_received_ts", _redactions_received_ts_txn
        )

        if not count:
            await self.db_pool.updates._end_background_update("redactions_received_ts")

        return count

    async def _event_fix_redactions_bytes(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Undoes hex encoded censored redacted event JSON."""

        def _event_fix_redactions_bytes_txn(txn: LoggingTransaction) -> None:
            # This update is quite fast due to new index.
            txn.execute(
                """
                UPDATE event_json
                SET
                    json = convert_from(json::bytea, 'utf8')
                FROM redactions
                WHERE
                    redactions.have_censored
                    AND event_json.event_id = redactions.redacts
                    AND json NOT LIKE '{%';
                """
            )

            txn.execute("DROP INDEX redactions_censored_redacts")

        await self.db_pool.runInteraction(
            "_event_fix_redactions_bytes", _event_fix_redactions_bytes_txn
        )

        await self.db_pool.updates._end_background_update("event_fix_redactions_bytes")

        return 1

    async def _event_store_labels(self, progress: JsonDict, batch_size: int) -> int:
        """Background update handler which will store labels for existing events."""
        last_event_id = progress.get("last_event_id", "")

        def _event_store_labels_txn(txn: LoggingTransaction) -> int:
            txn.execute(
                """
                SELECT event_id, json FROM event_json
                LEFT JOIN event_labels USING (event_id)
                WHERE event_id > ? AND label IS NULL
                ORDER BY event_id LIMIT ?
                """,
                (last_event_id, batch_size),
            )

            results = list(txn)

            nbrows = 0
            last_row_event_id = ""
            for event_id, event_json_raw in results:
                try:
                    event_json = db_to_json(event_json_raw)

                    self.db_pool.simple_insert_many_txn(
                        txn=txn,
                        table="event_labels",
                        keys=("event_id", "label", "room_id", "topological_ordering"),
                        values=[
                            (
                                event_id,
                                label,
                                event_json["room_id"],
                                event_json["depth"],
                            )
                            for label in event_json["content"].get(
                                EventContentFields.LABELS, []
                            )
                            if isinstance(label, str)
                        ],
                    )
                except Exception as e:
                    logger.warning(
                        "Unable to load event %s (no labels will be imported): %s",
                        event_id,
                        e,
                    )

                nbrows += 1
                last_row_event_id = event_id

            self.db_pool.updates._background_update_progress_txn(
                txn, "event_store_labels", {"last_event_id": last_row_event_id}
            )

            return nbrows

        num_rows = await self.db_pool.runInteraction(
            desc="event_store_labels", func=_event_store_labels_txn
        )

        if not num_rows:
            await self.db_pool.updates._end_background_update("event_store_labels")

        return num_rows

    async def _rejected_events_metadata(self, progress: dict, batch_size: int) -> int:
        """Adds rejected events to the `state_events` and `event_auth` metadata
        tables.
        """

        last_event_id = progress.get("last_event_id", "")

        def get_rejected_events(
            txn: Cursor,
        ) -> List[Tuple[str, str, JsonDict, bool, bool]]:
            # Fetch rejected event json, their room version and whether we have
            # inserted them into the state_events or auth_events tables.
            #
            # Note we can assume that events that don't have a corresponding
            # room version are V1 rooms.
            sql = """
                SELECT DISTINCT
                    event_id,
                    COALESCE(room_version, '1'),
                    json,
                    state_events.event_id IS NOT NULL,
                    event_auth.event_id IS NOT NULL
                FROM rejections
                INNER JOIN event_json USING (event_id)
                LEFT JOIN rooms USING (room_id)
                LEFT JOIN state_events USING (event_id)
                LEFT JOIN event_auth USING (event_id)
                WHERE event_id > ?
                ORDER BY event_id
                LIMIT ?
            """

            txn.execute(
                sql,
                (
                    last_event_id,
                    batch_size,
                ),
            )

            return cast(
                List[Tuple[str, str, JsonDict, bool, bool]],
                [(row[0], row[1], db_to_json(row[2]), row[3], row[4]) for row in txn],
            )

        results = await self.db_pool.runInteraction(
            desc="_rejected_events_metadata_get", func=get_rejected_events
        )

        if not results:
            await self.db_pool.updates._end_background_update(
                "rejected_events_metadata"
            )
            return 0

        state_events = []
        auth_events = []
        for event_id, room_version, event_json, has_state, has_event_auth in results:
            last_event_id = event_id

            if has_state and has_event_auth:
                continue

            room_version_obj = KNOWN_ROOM_VERSIONS.get(room_version)
            if not room_version_obj:
                # We no longer support this room version, so we just ignore the
                # events entirely.
                logger.info(
                    "Ignoring event with unknown room version %r: %r",
                    room_version,
                    event_id,
                )
                continue

            event = make_event_from_dict(event_json, room_version_obj)

            if not event.is_state():
                continue

            if not has_state:
                state_events.append(
                    (event.event_id, event.room_id, event.type, event.state_key)
                )

            if not has_event_auth:
                # Old, dodgy, events may have duplicate auth events, which we
                # need to deduplicate as we have a unique constraint.
                for auth_id in set(event.auth_event_ids()):
                    auth_events.append((event.event_id, event.room_id, auth_id))

        if state_events:
            await self.db_pool.simple_insert_many(
                table="state_events",
                keys=("event_id", "room_id", "type", "state_key"),
                values=state_events,
                desc="_rejected_events_metadata_state_events",
            )

        if auth_events:
            await self.db_pool.simple_insert_many(
                table="event_auth",
                keys=("event_id", "room_id", "auth_id"),
                values=auth_events,
                desc="_rejected_events_metadata_event_auth",
            )

        await self.db_pool.updates._background_update_progress(
            "rejected_events_metadata", {"last_event_id": last_event_id}
        )

        if len(results) < batch_size:
            await self.db_pool.updates._end_background_update(
                "rejected_events_metadata"
            )

        return len(results)

    async def _chain_cover_index(self, progress: dict, batch_size: int) -> int:
        """A background updates that iterates over all rooms and generates the
        chain cover index for them.
        """

        current_room_id = progress.get("current_room_id", "")

        # Where we've processed up to in the room, defaults to the start of the
        # room.
        last_depth = progress.get("last_depth", -1)
        last_stream = progress.get("last_stream", -1)

        result = await self.db_pool.runInteraction(
            "_chain_cover_index",
            self._calculate_chain_cover_txn,
            current_room_id,
            last_depth,
            last_stream,
            batch_size,
            single_room=False,
        )

        finished = result.processed_count == 0

        total_rows_processed = result.processed_count
        current_room_id = result.room_id
        last_depth = result.depth
        last_stream = result.stream

        for room_id, (depth, stream) in result.finished_room_map.items():
            # If we've done all the events in the room we flip the
            # `has_auth_chain_index` in the DB. Note that its possible for
            # further events to be persisted between the above and setting the
            # flag without having the chain cover calculated for them. This is
            # fine as a) the code gracefully handles these cases and b) we'll
            # calculate them below.

            await self.db_pool.simple_update(
                table="rooms",
                keyvalues={"room_id": room_id},
                updatevalues={"has_auth_chain_index": True},
                desc="_chain_cover_index",
            )

            # Handle any events that might have raced with us flipping the
            # bit above.
            result = await self.db_pool.runInteraction(
                "_chain_cover_index",
                self._calculate_chain_cover_txn,
                room_id,
                depth,
                stream,
                batch_size=None,
                single_room=True,
            )

            total_rows_processed += result.processed_count

        if finished:
            await self.db_pool.updates._end_background_update("chain_cover")
            return total_rows_processed

        await self.db_pool.updates._background_update_progress(
            "chain_cover",
            {
                "current_room_id": current_room_id,
                "last_depth": last_depth,
                "last_stream": last_stream,
            },
        )

        return total_rows_processed

    def _calculate_chain_cover_txn(
        self,
        txn: LoggingTransaction,
        last_room_id: str,
        last_depth: int,
        last_stream: int,
        batch_size: Optional[int],
        single_room: bool,
    ) -> _CalculateChainCover:
        """Calculate the chain cover for `batch_size` events, ordered by
        `(room_id, depth, stream)`.

        Args:
            txn,
            last_room_id, last_depth, last_stream: The `(room_id, depth, stream)`
                tuple to fetch results after.
            batch_size: The maximum number of events to process. If None then
                no limit.
            single_room: Whether to calculate the index for just the given
                room.
        """

        # Get the next set of events in the room (that we haven't already
        # computed chain cover for). We do this in topological order.

        # We want to do a `(topological_ordering, stream_ordering) > (?,?)`
        # comparison, but that is not supported on older SQLite versions
        tuple_clause, tuple_args = make_tuple_comparison_clause(
            [
                ("events.room_id", last_room_id),
                ("topological_ordering", last_depth),
                ("stream_ordering", last_stream),
            ],
        )

        extra_clause = ""
        if single_room:
            extra_clause = "AND events.room_id = ?"
            tuple_args.append(last_room_id)

        sql = """
            SELECT
                event_id, state_events.type, state_events.state_key,
                topological_ordering, stream_ordering,
                events.room_id
            FROM events
            INNER JOIN state_events USING (event_id)
            LEFT JOIN event_auth_chains USING (event_id)
            LEFT JOIN event_auth_chain_to_calculate USING (event_id)
            WHERE event_auth_chains.event_id IS NULL
                AND event_auth_chain_to_calculate.event_id IS NULL
                AND %(tuple_cmp)s
                %(extra)s
            ORDER BY events.room_id, topological_ordering, stream_ordering
            %(limit)s
        """ % {
            "tuple_cmp": tuple_clause,
            "limit": "LIMIT ?" if batch_size is not None else "",
            "extra": extra_clause,
        }

        if batch_size is not None:
            tuple_args.append(batch_size)

        txn.execute(sql, tuple_args)
        rows = txn.fetchall()

        # Put the results in the necessary format for
        # `_add_chain_cover_index`
        event_to_room_id = {row[0]: row[5] for row in rows}
        event_to_types = {row[0]: (row[1], row[2]) for row in rows}

        # Calculate the new last position we've processed up to.
        new_last_depth: int = rows[-1][3] if rows else last_depth
        new_last_stream: int = rows[-1][4] if rows else last_stream
        new_last_room_id: str = rows[-1][5] if rows else ""

        # Map from room_id to last depth/stream_ordering processed for the room,
        # excluding the last room (which we're likely still processing). We also
        # need to include the room passed in if it's not included in the result
        # set (as we then know we've processed all events in said room).
        #
        # This is the set of rooms that we can now safely flip the
        # `has_auth_chain_index` bit for.
        finished_rooms = {
            row[5]: (row[3], row[4]) for row in rows if row[5] != new_last_room_id
        }
        if last_room_id not in finished_rooms and last_room_id != new_last_room_id:
            finished_rooms[last_room_id] = (last_depth, last_stream)

        count = len(rows)

        # We also need to fetch the auth events for them.
        auth_events = cast(
            List[Tuple[str, str]],
            self.db_pool.simple_select_many_txn(
                txn,
                table="event_auth",
                column="event_id",
                iterable=event_to_room_id,
                keyvalues={},
                retcols=("event_id", "auth_id"),
            ),
        )

        event_to_auth_chain: Dict[str, List[str]] = {}
        for event_id, auth_id in auth_events:
            event_to_auth_chain.setdefault(event_id, []).append(auth_id)

        # Calculate and persist the chain cover index for this set of events.
        #
        # Annoyingly we need to gut wrench into the persit event store so that
        # we can reuse the function to calculate the chain cover for rooms.
        PersistEventsStore._add_chain_cover_index(
            txn,
            self.db_pool,
            self.event_chain_id_gen,
            event_to_room_id,
            event_to_types,
            cast(Dict[str, StrCollection], event_to_auth_chain),
        )

        return _CalculateChainCover(
            room_id=new_last_room_id,
            depth=new_last_depth,
            stream=new_last_stream,
            processed_count=count,
            finished_room_map=finished_rooms,
        )

    async def _purged_chain_cover_index(self, progress: dict, batch_size: int) -> int:
        """
        A background updates that iterates over the chain cover and deletes the
        chain cover for events that have been purged.

        This may be due to fully purging a room or via setting a retention policy.
        """
        current_event_id = progress.get("current_event_id", "")

        def purged_chain_cover_txn(txn: LoggingTransaction) -> int:
            # The event ID from events will be null if the chain ID / sequence
            # number points to a purged event.
            sql = """
                SELECT event_id, chain_id, sequence_number, e.event_id IS NOT NULL
                FROM event_auth_chains
                LEFT JOIN events AS e USING (event_id)
                WHERE event_id > ? ORDER BY event_auth_chains.event_id ASC LIMIT ?
            """
            txn.execute(sql, (current_event_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            # The event IDs and chain IDs / sequence numbers where the event has
            # been purged.
            unreferenced_event_ids = []
            unreferenced_chain_id_tuples = []
            event_id = ""
            for event_id, chain_id, sequence_number, has_event in rows:
                if not has_event:
                    unreferenced_event_ids.append((event_id,))
                    unreferenced_chain_id_tuples.append((chain_id, sequence_number))

            # Delete the unreferenced auth chains from event_auth_chain_links and
            # event_auth_chains.
            txn.executemany(
                """
                DELETE FROM event_auth_chains WHERE event_id = ?
                """,
                unreferenced_event_ids,
            )
            # We should also delete matching target_*, but there is no index on
            # target_chain_id. Hopefully any purged events are due to a room
            # being fully purged and they will be removed from the origin_*
            # searches.
            txn.executemany(
                """
                DELETE FROM event_auth_chain_links WHERE
                origin_chain_id = ? AND origin_sequence_number = ?
                """,
                unreferenced_chain_id_tuples,
            )

            progress = {
                "current_event_id": event_id,
            }

            self.db_pool.updates._background_update_progress_txn(
                txn, "purged_chain_cover", progress
            )

            return len(rows)

        result = await self.db_pool.runInteraction(
            "_purged_chain_cover_index",
            purged_chain_cover_txn,
        )

        if not result:
            await self.db_pool.updates._end_background_update("purged_chain_cover")

        return result

    async def _event_arbitrary_relations(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Background update handler which will store previously unknown relations for existing events."""
        last_event_id = progress.get("last_event_id", "")

        def _event_arbitrary_relations_txn(txn: LoggingTransaction) -> int:
            # Fetch events and then filter based on whether the event has a
            # relation or not.
            txn.execute(
                """
                SELECT event_id, json FROM event_json
                WHERE event_id > ?
                ORDER BY event_id LIMIT ?
                """,
                (last_event_id, batch_size),
            )

            results = list(txn)
            # (event_id, parent_id, rel_type) for each relation
            relations_to_insert: List[Tuple[str, str, str, str]] = []
            for event_id, event_json_raw in results:
                try:
                    event_json = db_to_json(event_json_raw)
                except Exception as e:
                    logger.warning(
                        "Unable to load event %s (no relations will be updated): %s",
                        event_id,
                        e,
                    )
                    continue

                # If there's no relation, skip!
                relates_to = event_json["content"].get("m.relates_to")
                if not relates_to or not isinstance(relates_to, dict):
                    continue

                # If the relation type or parent event ID is not a string, skip it.
                #
                # Do not consider relation types that have existed for a long time,
                # since they will already be listed in the `event_relations` table.
                rel_type = relates_to.get("rel_type")
                if not isinstance(rel_type, str) or rel_type in (
                    RelationTypes.ANNOTATION,
                    RelationTypes.REFERENCE,
                    RelationTypes.REPLACE,
                ):
                    continue

                parent_id = relates_to.get("event_id")
                if not isinstance(parent_id, str):
                    continue

                room_id = event_json["room_id"]
                relations_to_insert.append((room_id, event_id, parent_id, rel_type))

            # Insert the missing data, note that we upsert here in case the event
            # has already been processed.
            if relations_to_insert:
                self.db_pool.simple_upsert_many_txn(
                    txn=txn,
                    table="event_relations",
                    key_names=("event_id",),
                    key_values=[(r[1],) for r in relations_to_insert],
                    value_names=("relates_to_id", "relation_type"),
                    value_values=[r[2:] for r in relations_to_insert],
                )

                # Iterate the parent IDs and invalidate caches.
                self._invalidate_cache_and_stream_bulk(  # type: ignore[attr-defined]
                    txn,
                    self.get_relations_for_event,  # type: ignore[attr-defined]
                    {
                        (
                            r[0],  # room_id
                            r[2],  # parent_id
                        )
                        for r in relations_to_insert
                    },
                )
                self._invalidate_cache_and_stream_bulk(  # type: ignore[attr-defined]
                    txn,
                    self.get_thread_summary,  # type: ignore[attr-defined]
                    {(r[1],) for r in relations_to_insert},
                )

            if results:
                latest_event_id = results[-1][0]
                self.db_pool.updates._background_update_progress_txn(
                    txn, "event_arbitrary_relations", {"last_event_id": latest_event_id}
                )

            return len(results)

        num_rows = await self.db_pool.runInteraction(
            desc="event_arbitrary_relations", func=_event_arbitrary_relations_txn
        )

        if not num_rows:
            await self.db_pool.updates._end_background_update(
                "event_arbitrary_relations"
            )

        return num_rows

    async def _background_populate_stream_ordering2(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Populate events.stream_ordering2, then replace stream_ordering

        This is to deal with the fact that stream_ordering was initially created as a
        32-bit integer field.
        """
        batch_size = max(batch_size, 1)

        def process(txn: LoggingTransaction) -> int:
            last_stream = progress.get("last_stream", -(1 << 31))
            txn.execute(
                """
                UPDATE events SET stream_ordering2=stream_ordering
                WHERE stream_ordering IN (
                   SELECT stream_ordering FROM events WHERE stream_ordering > ?
                   ORDER BY stream_ordering LIMIT ?
                )
                RETURNING stream_ordering;
                """,
                (last_stream, batch_size),
            )
            row_count = txn.rowcount
            if row_count == 0:
                return 0
            last_stream = max(row[0] for row in txn)
            logger.info("populated stream_ordering2 up to %i", last_stream)

            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.POPULATE_STREAM_ORDERING2,
                {"last_stream": last_stream},
            )
            return row_count

        result = await self.db_pool.runInteraction(
            "_background_populate_stream_ordering2", process
        )

        if result != 0:
            return result

        await self.db_pool.updates._end_background_update(
            _BackgroundUpdates.POPULATE_STREAM_ORDERING2
        )
        return 0

    async def _background_replace_stream_ordering_column(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Drop the old 'stream_ordering' column and rename 'stream_ordering2' into its place."""

        def process(txn: Cursor) -> None:
            for sql in _REPLACE_STREAM_ORDERING_SQL_COMMANDS:
                logger.info("completing stream_ordering migration: %s", sql)
                txn.execute(sql)

        # ANALYZE the new column to build stats on it, to encourage PostgreSQL to use the
        # indexes on it.
        await self.db_pool.runInteraction(
            "background_analyze_new_stream_ordering_column",
            lambda txn: txn.execute("ANALYZE events(stream_ordering2)"),
        )

        await self.db_pool.runInteraction(
            "_background_replace_stream_ordering_column", process
        )

        await self.db_pool.updates._end_background_update(
            _BackgroundUpdates.REPLACE_STREAM_ORDERING_COLUMN
        )

        return 0

    async def _background_drop_invalid_event_edges_rows(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Drop invalid rows from event_edges

        This only runs for postgres. For SQLite, it all happens synchronously.

        Firstly, drop any rows with is_state=True. These may have been added a long time
        ago, but they are no longer used.

        We also drop rows that do not correspond to entries in `events`, and add a
        foreign key.
        """

        last_event_id = progress.get("last_event_id", "")

        def drop_invalid_event_edges_txn(txn: LoggingTransaction) -> bool:
            """Returns True if we're done."""

            # first we need to find an endpoint.
            txn.execute(
                """
                SELECT event_id FROM event_edges
                WHERE event_id > ?
                ORDER BY event_id
                LIMIT 1 OFFSET ?
                """,
                (last_event_id, batch_size),
            )

            endpoint = None
            row = txn.fetchone()

            if row:
                endpoint = row[0]

            where_clause = "ee.event_id > ?"
            args = [last_event_id]
            if endpoint:
                where_clause += " AND ee.event_id <= ?"
                args.append(endpoint)

            # now delete any that:
            #   - have is_state=TRUE, or
            #   - do not correspond to a row in `events`
            txn.execute(
                f"""
                DELETE FROM event_edges
                WHERE event_id IN (
                   SELECT ee.event_id
                   FROM event_edges ee
                     LEFT JOIN events ev USING (event_id)
                   WHERE ({where_clause}) AND
                     (is_state OR ev.event_id IS NULL)
                )""",
                args,
            )

            logger.info(
                "cleaned up event_edges up to %s: removed %i/%i rows",
                endpoint,
                txn.rowcount,
                batch_size,
            )

            if endpoint is not None:
                self.db_pool.updates._background_update_progress_txn(
                    txn,
                    _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS,
                    {"last_event_id": endpoint},
                )
                return False

            # if that was the final batch, we validate the foreign key.
            #
            # The constraint should have been in place and enforced for new rows since
            # before we started deleting invalid rows, so there's no chance for any
            # invalid rows to have snuck in the meantime. In other words, this really
            # ought to succeed.
            logger.info("cleaned up event_edges; enabling foreign key")
            txn.execute(
                "ALTER TABLE event_edges VALIDATE CONSTRAINT event_edges_event_id_fkey"
            )
            return True

        done = await self.db_pool.runInteraction(
            desc="drop_invalid_event_edges", func=drop_invalid_event_edges_txn
        )

        if done:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENT_EDGES_DROP_INVALID_ROWS
            )

        return batch_size

    async def _background_events_populate_state_key_rejections(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """Back-populate `events.state_key` and `events.rejection_reason"""

        min_stream_ordering_exclusive = progress["min_stream_ordering_exclusive"]
        max_stream_ordering_inclusive = progress["max_stream_ordering_inclusive"]

        def _populate_txn(txn: LoggingTransaction) -> bool:
            """Returns True if we're done."""

            # first we need to find an endpoint.
            # we need to find the final row in the batch of batch_size, which means
            # we need to skip over (batch_size-1) rows and get the next row.
            txn.execute(
                """
                SELECT stream_ordering FROM events
                WHERE stream_ordering > ? AND stream_ordering <= ?
                ORDER BY stream_ordering
                LIMIT 1 OFFSET ?
                """,
                (
                    min_stream_ordering_exclusive,
                    max_stream_ordering_inclusive,
                    batch_size - 1,
                ),
            )

            row = txn.fetchone()
            if row:
                endpoint = row[0]
            else:
                # if the query didn't return a row, we must be almost done. We just
                # need to go up to the recorded max_stream_ordering.
                endpoint = max_stream_ordering_inclusive

            where_clause = "stream_ordering > ? AND stream_ordering <= ?"
            args = [min_stream_ordering_exclusive, endpoint]

            # now do the updates.
            txn.execute(
                f"""
                UPDATE events
                SET state_key = (SELECT state_key FROM state_events se WHERE se.event_id = events.event_id),
                    rejection_reason = (SELECT reason FROM rejections rej WHERE rej.event_id = events.event_id)
                WHERE ({where_clause})
                """,
                args,
            )

            logger.info(
                "populated new `events` columns up to %i/%i: updated %i rows",
                endpoint,
                max_stream_ordering_inclusive,
                txn.rowcount,
            )

            if endpoint >= max_stream_ordering_inclusive:
                # we're done
                return True

            progress["min_stream_ordering_exclusive"] = endpoint
            self.db_pool.updates._background_update_progress_txn(
                txn,
                _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS,
                progress,
            )
            return False

        done = await self.db_pool.runInteraction(
            desc="events_populate_state_key_rejections", func=_populate_txn
        )

        if done:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.EVENTS_POPULATE_STATE_KEY_REJECTIONS
            )

        return batch_size

    async def _sliding_sync_joined_rooms_backfill(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Handles backfilling the `sliding_sync_joined_rooms` table.
        """
        last_room_id = progress.get("last_room_id", "")

        def _get_rooms_to_update_txn(txn: LoggingTransaction) -> List[str]:
            # Fetch the set of room IDs that we want to update
            txn.execute(
                """
                SELECT DISTINCT room_id FROM current_state_events
                WHERE room_id > ?
                ORDER BY room_id ASC
                LIMIT ?
                """,
                (last_room_id, batch_size),
            )

            rooms_to_update_rows = cast(List[Tuple[str]], txn.fetchall())

            return [row[0] for row in rooms_to_update_rows]

        rooms_to_update = await self.db_pool.runInteraction(
            "_sliding_sync_joined_rooms_backfill._get_rooms_to_update_txn",
            _get_rooms_to_update_txn,
        )

        if not rooms_to_update:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BACKFILL
            )
            return 0

        # Map from room_id to insert/update state values in the `sliding_sync_joined_rooms` table
        joined_room_updates: Dict[str, SlidingSyncStateInsertValues] = {}
        # Map from room_id to stream_ordering/bump_stamp/last_current_state_delta_stream_id values
        joined_room_stream_ordering_updates: Dict[str, Tuple[int, int, int]] = {}
        for room_id in rooms_to_update:
            current_state_ids_map, last_current_state_delta_stream_id = (
                await self.db_pool.runInteraction(
                    "_sliding_sync_joined_rooms_backfill._get_relevant_sliding_sync_current_state_event_ids_txn",
                    PersistEventsStore._get_relevant_sliding_sync_current_state_event_ids_txn,
                    room_id,
                )
            )
            # We're iterating over rooms pulled from the current_state_events table
            # so we should have some current state for each room
            assert current_state_ids_map

            fetched_events = await self.get_events(current_state_ids_map.values())

            current_state_map: StateMap[EventBase] = {
                state_key: fetched_events[event_id]
                for state_key, event_id in current_state_ids_map.items()
            }

            state_insert_values = (
                PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                    current_state_map
                )
            )
            # We should have some insert values for each room, even if they are `None`
            assert state_insert_values
            joined_room_updates[room_id] = state_insert_values

            # Figure out the stream_ordering of the latest event in the room
            most_recent_event_pos_results = await self.get_last_event_pos_in_room(
                room_id, event_types=None
            )
            assert most_recent_event_pos_results, (
                f"We should not be seeing `None` here because the room ({room_id}) should at-least have a create event "
                + "given we pulled the room out of `current_state_events`"
            )
            # Figure out the latest bump_stamp in the room
            bump_stamp_event_pos_results = await self.get_last_event_pos_in_room(
                room_id, event_types=SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES
            )
            assert bump_stamp_event_pos_results, (
                f"We should not be seeing `None` here because the room ({room_id}) should at-least have a create event "
                + "(unless `SLIDING_SYNC_DEFAULT_BUMP_EVENT_TYPES` no longer includes the room create event)"
            )
            joined_room_stream_ordering_updates[room_id] = (
                most_recent_event_pos_results[1].stream,
                bump_stamp_event_pos_results[1].stream,
                last_current_state_delta_stream_id,
            )

        def _backfill_table_txn(txn: LoggingTransaction) -> None:
            # Handle updating the `sliding_sync_joined_rooms` table
            #
            last_successful_room_id: Optional[str] = None
            for room_id, insert_map in joined_room_updates.items():
                (
                    event_stream_ordering,
                    bump_stamp,
                    last_current_state_delta_stream_id,
                ) = joined_room_stream_ordering_updates[room_id]

                # Check if the current state has been updated since we gathered it
                state_deltas_since_we_gathered_current_state = (
                    self.get_current_state_deltas_for_room_txn(
                        txn,
                        room_id,
                        from_token=RoomStreamToken(
                            stream=last_current_state_delta_stream_id
                        ),
                        to_token=None,
                    )
                )
                for state_delta in state_deltas_since_we_gathered_current_state:
                    # We only need to check if the state is relevant to the
                    # `sliding_sync_joined_rooms` table.
                    if (
                        state_delta.event_type,
                        state_delta.state_key,
                    ) in SLIDING_SYNC_RELEVANT_STATE_SET:
                        # Save our progress before we exit early
                        if last_successful_room_id is not None:
                            self.db_pool.updates._background_update_progress_txn(
                                txn,
                                _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BACKFILL,
                                {"last_room_id": room_id},
                            )
                        # Raising exception so we can just exit and try again. It would
                        # be hard to resolve this within the transaction because we need
                        # to get full events out that take redactions into account. We
                        # could add some retry logic here, but it's easier to just let
                        # the background update try again.
                        raise Exception(
                            "Current state was updated after we gathered it to update "
                            + "`sliding_sync_joined_rooms` in the background update. "
                            + "Raising exception so we can just try again."
                        )

                # Pulling keys/values separately is safe and will produce congruent
                # lists
                insert_keys = insert_map.keys()
                insert_values = insert_map.values()
                # Since we partially update the `sliding_sync_joined_rooms` as new state
                # is sent, we need to update the state fields `ON CONFLICT`. We just
                # have to be careful we're not overwriting it with stale data (see
                # `last_current_state_delta_stream_id` check above).
                #
                # We don't need to update `event_stream_ordering` and `bump_stamp` `ON
                # CONFLICT` because if they are present, that means they are already
                # up-to-date.
                sql = f"""
                    INSERT INTO sliding_sync_joined_rooms
                        (room_id, event_stream_ordering, bump_stamp, {", ".join(insert_keys)})
                    VALUES (
                        ?, ?, ?,
                        {", ".join("?" for _ in insert_values)}
                    )
                    ON CONFLICT (room_id)
                    DO UPDATE SET
                        {", ".join(f"{key} = EXCLUDED.{key}" for key in insert_keys)}
                    """
                args = [room_id, event_stream_ordering, bump_stamp] + list(
                    insert_values
                )
                txn.execute(sql, args)

                # Keep track of the last successful room_id
                last_successful_room_id = room_id

        await self.db_pool.runInteraction(
            "sliding_sync_joined_rooms_backfill", _backfill_table_txn
        )

        # Update the progress
        await self.db_pool.updates._background_update_progress(
            _BackgroundUpdates.SLIDING_SYNC_JOINED_ROOMS_BACKFILL,
            {"last_room_id": rooms_to_update[-1]},
        )

        return len(rooms_to_update)

    async def _sliding_sync_membership_snapshots_backfill(
        self, progress: JsonDict, batch_size: int
    ) -> int:
        """
        Handles backfilling the `sliding_sync_membership_snapshots` table.
        """
        last_event_stream_ordering = progress.get(
            "last_event_stream_ordering", -(1 << 31)
        )

        def _find_memberships_to_update_txn(
            txn: LoggingTransaction,
        ) -> List[Tuple[str, str, str, str, str, int, bool]]:
            # Fetch the set of event IDs that we want to update
            txn.execute(
                """
                SELECT
                    c.room_id,
                    c.user_id,
                    e.sender,
                    c.event_id,
                    c.membership,
                    c.event_stream_ordering,
                    e.outlier
                FROM local_current_membership as c
                INNER JOIN events AS e USING (event_id)
                WHERE event_stream_ordering > ?
                ORDER BY event_stream_ordering ASC
                LIMIT ?
                """,
                (last_event_stream_ordering, batch_size),
            )

            memberships_to_update_rows = cast(
                List[Tuple[str, str, str, str, str, int, bool]], txn.fetchall()
            )

            return memberships_to_update_rows

        memberships_to_update_rows = await self.db_pool.runInteraction(
            "sliding_sync_membership_snapshots_backfill._find_memberships_to_update_txn",
            _find_memberships_to_update_txn,
        )

        if not memberships_to_update_rows:
            await self.db_pool.updates._end_background_update(
                _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BACKFILL
            )
            return 0

        def _find_previous_membership_txn(
            txn: LoggingTransaction, room_id: str, user_id: str, stream_ordering: int
        ) -> Tuple[str, str]:
            # Find the previous invite/knock event before the leave event
            txn.execute(
                """
                SELECT event_id, membership
                FROM room_memberships
                WHERE
                    room_id = ?
                    AND user_id = ?
                    AND event_stream_ordering < ?
                ORDER BY event_stream_ordering DESC
                LIMIT 1
                """,
                (
                    room_id,
                    user_id,
                    stream_ordering,
                ),
            )
            row = txn.fetchone()

            # We should see a corresponding previous invite/knock event
            assert row is not None
            event_id, membership = row

            return event_id, membership

        # Map from (room_id, user_id) to ...
        to_insert_membership_snapshots: Dict[
            Tuple[str, str], SlidingSyncMembershipSnapshotSharedInsertValues
        ] = {}
        to_insert_membership_infos: Dict[Tuple[str, str], SlidingSyncMembershipInfo] = (
            {}
        )
        for (
            room_id,
            user_id,
            sender,
            membership_event_id,
            membership,
            membership_event_stream_ordering,
            is_outlier,
        ) in memberships_to_update_rows:
            # We don't know how to handle `membership` values other than these. The
            # code below would need to be updated.
            assert membership in (
                Membership.JOIN,
                Membership.INVITE,
                Membership.KNOCK,
                Membership.LEAVE,
                Membership.BAN,
            )

            # Map of values to insert/update in the `sliding_sync_membership_snapshots` table
            sliding_sync_membership_snapshots_insert_map: (
                SlidingSyncMembershipSnapshotSharedInsertValues
            ) = {}
            if membership == Membership.JOIN:
                # If we're still joined, we can pull from current state.
                current_state_ids_map: StateMap[
                    str
                ] = await self.hs.get_storage_controllers().state.get_current_state_ids(
                    room_id,
                    state_filter=StateFilter.from_types(
                        SLIDING_SYNC_RELEVANT_STATE_SET
                    ),
                    # Partially-stated rooms should have all state events except for
                    # remote membership events so we don't need to wait at all because
                    # we only want some non-membership state
                    await_full_state=False,
                )
                # We're iterating over rooms that we are joined to so they should
                # have `current_state_events` and we should have some current state
                # for each room
                assert current_state_ids_map

                fetched_events = await self.get_events(current_state_ids_map.values())

                current_state_map: StateMap[EventBase] = {
                    state_key: fetched_events[event_id]
                    for state_key, event_id in current_state_ids_map.items()
                }

                state_insert_values = (
                    PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                        current_state_map
                    )
                )
                sliding_sync_membership_snapshots_insert_map.update(state_insert_values)
                # We should have some insert values for each room, even if they are `None`
                assert sliding_sync_membership_snapshots_insert_map

                # We have current state to work from
                sliding_sync_membership_snapshots_insert_map["has_known_state"] = True
            elif membership in (Membership.INVITE, Membership.KNOCK) or (
                membership == Membership.LEAVE and is_outlier
            ):
                invite_or_knock_event_id = membership_event_id
                invite_or_knock_membership = membership

                # If the event is an `out_of_band_membership` (special case of
                # `outlier`), we never had historical state so we have to pull from
                # the stripped state on the previous invite/knock event. This gives
                # us a consistent view of the room state regardless of your
                # membership (i.e. the room shouldn't disappear if your using the
                # `is_encrypted` filter and you leave).
                if membership == Membership.LEAVE and is_outlier:
                    invite_or_knock_event_id, invite_or_knock_membership = (
                        await self.db_pool.runInteraction(
                            "sliding_sync_membership_snapshots_backfill._find_previous_membership",
                            _find_previous_membership_txn,
                            room_id,
                            user_id,
                            membership_event_stream_ordering,
                        )
                    )

                # Pull from the stripped state on the invite/knock event
                invite_or_knock_event = await self.get_event(invite_or_knock_event_id)

                raw_stripped_state_events = None
                if invite_or_knock_membership == Membership.INVITE:
                    invite_room_state = invite_or_knock_event.unsigned.get(
                        "invite_room_state"
                    )
                    raw_stripped_state_events = invite_room_state
                elif invite_or_knock_membership == Membership.KNOCK:
                    knock_room_state = invite_or_knock_event.unsigned.get(
                        "knock_room_state"
                    )
                    raw_stripped_state_events = knock_room_state

                sliding_sync_membership_snapshots_insert_map = await self.db_pool.runInteraction(
                    "sliding_sync_membership_snapshots_backfill._get_sliding_sync_insert_values_from_stripped_state_txn",
                    PersistEventsStore._get_sliding_sync_insert_values_from_stripped_state_txn,
                    raw_stripped_state_events,
                )

                # We should have some insert values for each room, even if no
                # stripped state is on the event because we still want to record
                # that we have no known state
                assert sliding_sync_membership_snapshots_insert_map
            elif membership in (Membership.LEAVE, Membership.BAN):
                # Pull from historical state
                state_ids_map = await self.hs.get_storage_controllers().state.get_state_ids_for_event(
                    membership_event_id,
                    state_filter=StateFilter.from_types(
                        SLIDING_SYNC_RELEVANT_STATE_SET
                    ),
                    # Partially-stated rooms should have all state events except for
                    # remote membership events so we don't need to wait at all because
                    # we only want some non-membership state
                    await_full_state=False,
                )

                fetched_events = await self.get_events(state_ids_map.values())

                state_map: StateMap[EventBase] = {
                    state_key: fetched_events[event_id]
                    for state_key, event_id in state_ids_map.items()
                }

                state_insert_values = (
                    PersistEventsStore._get_sliding_sync_insert_values_from_state_map(
                        state_map
                    )
                )
                sliding_sync_membership_snapshots_insert_map.update(state_insert_values)
                # We should have some insert values for each room, even if they are `None`
                assert sliding_sync_membership_snapshots_insert_map

                # We have historical state to work from
                sliding_sync_membership_snapshots_insert_map["has_known_state"] = True
            else:
                # We don't know how to handle this type of membership yet
                #
                # FIXME: We should use `assert_never` here but for some reason
                # the exhaustive matching doesn't recognize the `Never` here.
                # assert_never(membership)
                raise AssertionError(
                    f"Unexpected membership {membership} ({membership_event_id}) that we don't know how to handle yet"
                )

            to_insert_membership_snapshots[(room_id, user_id)] = (
                sliding_sync_membership_snapshots_insert_map
            )
            to_insert_membership_infos[(room_id, user_id)] = SlidingSyncMembershipInfo(
                user_id=user_id,
                sender=sender,
                membership_event_id=membership_event_id,
                membership=membership,
                membership_event_stream_ordering=membership_event_stream_ordering,
            )

        def _backfill_table_txn(txn: LoggingTransaction) -> None:
            # Handle updating the `sliding_sync_membership_snapshots` table
            #
            for key, insert_map in to_insert_membership_snapshots.items():
                room_id, user_id = key
                membership_info = to_insert_membership_infos[key]
                sender = membership_info.sender
                membership_event_id = membership_info.membership_event_id
                membership = membership_info.membership
                membership_event_stream_ordering = (
                    membership_info.membership_event_stream_ordering
                )

                # Pulling keys/values separately is safe and will produce congruent
                # lists
                insert_keys = insert_map.keys()
                insert_values = insert_map.values()
                # We don't need to update the state `ON CONFLICT` because we never
                # partially insert/update the snapshots and anything already there is
                # up-to-date EXCEPT for the `forgotten` field since that is updated out
                # of band from the membership changes.
                #
                # We need to find the `forgotten` value during the transaction because
                # we can't risk inserting stale data.
                txn.execute(
                    f"""
                    INSERT INTO sliding_sync_membership_snapshots
                        (room_id, user_id, sender, membership_event_id, membership, forgotten, event_stream_ordering
                        {("," + ", ".join(insert_keys)) if insert_keys else ""})
                    VALUES (
                        ?, ?, ?, ?, ?,
                        (SELECT forgotten FROM room_memberships WHERE event_id = ?),
                        ?
                        {("," + ", ".join("?" for _ in insert_values)) if insert_values else ""}
                    )
                    ON CONFLICT (room_id, user_id)
                    DO UPDATE SET
                        forgotten = EXCLUDED.forgotten
                    """,
                    [
                        room_id,
                        user_id,
                        sender,
                        membership_event_id,
                        membership,
                        membership_event_id,
                        membership_event_stream_ordering,
                    ]
                    + list(insert_values),
                )

        await self.db_pool.runInteraction(
            "sliding_sync_membership_snapshots_backfill", _backfill_table_txn
        )

        # Update the progress
        (
            _room_id,
            _user_id,
            _sender,
            _membership_event_id,
            _membership,
            membership_event_stream_ordering,
            _is_outlier,
        ) = memberships_to_update_rows[-1]
        await self.db_pool.updates._background_update_progress(
            _BackgroundUpdates.SLIDING_SYNC_MEMBERSHIP_SNAPSHOTS_BACKFILL,
            {"last_event_stream_ordering": membership_event_stream_ordering},
        )

        return len(memberships_to_update_rows)
