import datetime
import json
from typing import Any, Dict, List, Optional, Tuple, Union

import structlog
from celery import group
from celery.canvas import Signature
from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core.cache import cache
from django.db.models import Q
from django.db.models.expressions import F
from django.db.models.query import QuerySet
from django.utils import timezone
from sentry_sdk import capture_exception
from statshog.defaults.django import statsd

from posthog.caching.calculate_results import (
    cache_includes_latest_events,
    calculate_result_by_cache_type,
    get_cache_type,
)
from posthog.caching.reporting import CacheUpdateReporting
from posthog.caching.utils import active_teams
from posthog.celery import update_cache_item_task
from posthog.decorators import CacheType
from posthog.logging.timing import timed
from posthog.models import Dashboard, DashboardTile, Filter, Insight, Team
from posthog.models.filters.utils import get_filter
from posthog.models.instance_setting import get_instance_setting
from posthog.utils import generate_cache_key

logger = structlog.get_logger(__name__)


def update_cached_items() -> Tuple[int, int]:
    PARALLEL_INSIGHT_CACHE = get_instance_setting("PARALLEL_DASHBOARD_ITEM_CACHE")
    recent_teams = active_teams()

    tasks: List[Optional[Signature]] = []

    dashboard_tiles = (
        DashboardTile.objects.filter(insight__team_id__in=recent_teams)
        .filter(
            Q(dashboard__sharingconfiguration__enabled=True)
            | Q(dashboard__last_accessed_at__gt=timezone.now() - relativedelta(days=7))
        )
        .filter(
            # no last refresh date or last refresh not in last three minutes
            Q(last_refresh__isnull=True)
            | Q(last_refresh__lt=timezone.now() - relativedelta(minutes=3))
        )
        .exclude(dashboard__deleted=True)
        .exclude(insight__deleted=True)
        .exclude(insight__filters={})
        .exclude(refreshing=True)
        .exclude(refresh_attempt__gt=2)
        .select_related("insight", "dashboard")
        .order_by(F("last_refresh").asc(nulls_first=True), F("refresh_attempt").asc())
    )

    for dashboard_tile in dashboard_tiles[0:PARALLEL_INSIGHT_CACHE]:
        tasks.append(task_for_cache_update_candidate(dashboard_tile))

    shared_insights = (
        Insight.objects.filter(team_id__in=recent_teams)
        .filter(sharingconfiguration__enabled=True)
        .exclude(deleted=True)
        .exclude(filters={})
        .exclude(refreshing=True)
        .exclude(refresh_attempt__gt=2)
        .order_by(F("last_refresh").asc(nulls_first=True))
    )

    for insight in shared_insights[0:PARALLEL_INSIGHT_CACHE]:
        tasks.append(task_for_cache_update_candidate(insight))

    gauge_cache_update_candidates(dashboard_tiles, shared_insights)

    tasks = list(filter(None, tasks))
    group(tasks).apply_async()
    return len(tasks), dashboard_tiles.count() + shared_insights.count()


def task_for_cache_update_candidate(candidate: Union[DashboardTile, Insight]) -> Optional[Signature]:
    candidate_tile: Optional[DashboardTile] = None if isinstance(candidate, Insight) else candidate
    candidate_insight: Optional[Insight] = candidate if isinstance(candidate, Insight) else candidate.insight
    if candidate_insight is None:
        return None

    candidate_dashboard: Optional[Dashboard] = None if isinstance(candidate, Insight) else candidate.dashboard

    if candidate_tile:
        last_refresh = candidate_tile.last_refresh
    else:
        last_refresh = candidate_insight.last_refresh

    try:
        cache_key, cache_type, payload = insight_update_task_params(
            candidate_insight, candidate_dashboard, last_refresh
        )
        update_filters_hash(cache_key, candidate_dashboard, candidate_insight)
        return update_cache_item_task.s(cache_key, cache_type, payload)
    except Exception as e:
        candidate_insight.refresh_attempt = (candidate_insight.refresh_attempt or 0) + 1
        candidate_insight.save(update_fields=["refresh_attempt"])
        if candidate_tile:
            candidate_tile.refresh_attempt = (candidate_tile.refresh_attempt or 0) + 1
            candidate_tile.save(update_fields=["refresh_attempt"])
        capture_exception(e)
        return None


def gauge_cache_update_candidates(dashboard_tiles: QuerySet, shared_insights: QuerySet) -> None:
    statsd.gauge(
        "update_cache_queue.never_refreshed", dashboard_tiles.exclude(insight=None).filter(last_refresh=None).count()
    )
    oldest_previously_refreshed_tiles: List[DashboardTile] = list(
        dashboard_tiles.exclude(insight=None).exclude(last_refresh=None)[0:10]
    )
    ages = []
    for candidate_tile in oldest_previously_refreshed_tiles:
        if candidate_tile.insight_id is None:
            continue

        dashboard_cache_age = (datetime.datetime.now(timezone.utc) - candidate_tile.last_refresh).total_seconds()

        tags = {
            "insight_id": candidate_tile.insight_id,
            "dashboard_id": candidate_tile.dashboard_id,
            "cache_key": candidate_tile.filters_hash,
        }
        statsd.gauge("update_cache_queue.dashboards_lag", round(dashboard_cache_age), tags=tags)
        ages.append({**tags, "age": round(dashboard_cache_age)})

    logger.info("update_cache_queue.seen_ages", ages=ages)

    # this is the number of cacheable items that match the query
    statsd.gauge("update_cache_queue_depth.shared_insights", shared_insights.count())
    statsd.gauge("update_cache_queue_depth.dashboards", dashboard_tiles.count())
    statsd.gauge("update_cache_queue_depth", dashboard_tiles.count() + shared_insights.count())


@timed("update_cache_item_timer")
def update_cache_item(key: str, cache_type: CacheType, payload: dict) -> Optional[List[Dict[str, Any]]]:

    dashboard_id = payload.get("dashboard_id", None)
    insight_id = payload.get("insight_id", "unknown")
    filter_dict = json.loads(payload["filter"])
    team_id = int(payload["team_id"])

    team = Team.objects.get(pk=team_id)
    filter = get_filter(data=filter_dict, team=team)

    insights_queryset = Insight.objects.filter(Q(team_id=team_id, filters_hash=key))
    insights_queryset.update(refreshing=True)
    dashboard_tiles_queryset = DashboardTile.objects.filter(insight__team_id=team_id, filters_hash=key)
    dashboard_tiles_queryset.update(refreshing=True)

    cache_update_reporting = CacheUpdateReporting(
        dashboard_id=dashboard_id,
        dashboard_tiles_queryset=dashboard_tiles_queryset,
        insight_id=insight_id,
        insights_queryset=insights_queryset,
        key=key,
        team=team,
    )

    result = None

    if cache_includes_latest_events(payload, filter):
        cache.touch(key, timeout=settings.CACHED_RESULTS_TTL)
        cache_update_reporting.on_results("update_cache_item_can_skip_because_events_do_not_invalidate_cache")
    else:
        try:
            if (dashboard_id and dashboard_tiles_queryset.exists()) or insights_queryset.exists():
                result = _update_cache_for_queryset(cache_type, filter, key, team)
        except Exception as e:
            cache_update_reporting.on_query_error(e)
            raise e

        if result:
            cache_update_reporting.on_results("update_cache_item_success")
        else:
            cache_update_reporting.on_no_results()
            result = []

    logger.info(
        "update_insight_cache.processed_item",
        insight_id=payload.get("insight_id", None),
        dashboard_id=payload.get("dashboard_id", None),
        cache_key=key,
        has_results=result and len(result) > 0,
    )

    return result


def _update_cache_for_queryset(
    cache_type: CacheType, filter: Filter, key: str, team: Team
) -> Optional[List[Dict[str, Any]]]:
    result = calculate_result_by_cache_type(cache_type, filter, team)

    cache.set(key, {"result": result, "type": cache_type, "last_refresh": timezone.now()}, settings.CACHED_RESULTS_TTL)

    return result


def synchronously_update_insight_cache(insight: Insight, dashboard: Optional[Dashboard]) -> List[Dict[str, Any]]:
    cache_key, cache_type, payload = insight_update_task_params(insight, dashboard)
    update_filters_hash(cache_key, dashboard, insight)
    result = update_cache_item(cache_key, cache_type, payload)
    insight.refresh_from_db()
    return result


def update_filters_hash(cache_key: str, dashboard: Optional[Dashboard], insight: Insight) -> None:
    """check if the cache key has changed, usually because of a new default filter
    # there are three possibilities
    # 1) the insight is not being updated in a dashboard context
    #    --> so set its cache key if it doesn't match
    # 2) the insight is being updated in a dashboard context and the dashboard has different filters to the insight
    #    --> so set only the dashboard tile's filters_hash
    # 3) the insight is being updated in a dashboard context and the dashboard has matching or no filters
    #    --> so set the dashboard tile and the insight's filters hash"""

    should_update_insight_filters_hash = False

    if not dashboard and insight.filters_hash and insight.filters_hash != cache_key:
        logger.info(
            "update_cache_shared_insight_incorrect_filters_hash",
            current_cache_key=insight.filters_hash,
            correct_cache_key=cache_key,
        )
        should_update_insight_filters_hash = True
    if dashboard:
        dashboard_tiles = DashboardTile.objects.filter(insight=insight, dashboard=dashboard).exclude(
            filters_hash=cache_key
        )

        count_of_updated_tiles = dashboard_tiles.update(filters_hash=cache_key)
        if count_of_updated_tiles:
            logger.info(
                "update_cache_dashboard_tile_incorrect_filters_hash",
                current_cache_keys=[dt.filters_hash for dt in dashboard_tiles],
                correct_cache_key=cache_key,
            )

            if not dashboard.filters or dashboard.filters == insight.filters:
                should_update_insight_filters_hash = True

            statsd.incr(
                "update_cache_item_set_new_cache_key_on_tile",
                count=count_of_updated_tiles,
                tags={
                    "team": insight.team.id,
                    "cache_key": cache_key,
                    "insight_id": insight.id,
                    "dashboard_id": dashboard.id,
                },
            )
    if should_update_insight_filters_hash:
        insight.filters_hash = cache_key
        insight.save()

        statsd.incr(
            "update_cache_item_set_new_cache_key_on_shared_insight",
            tags={"team": insight.team.id, "cache_key": cache_key, "insight_id": insight.id, "dashboard_id": None},
        )


def insight_update_task_params(
    insight: Insight, dashboard: Optional[Dashboard] = None, last_refresh: Optional[datetime.datetime] = None
) -> Tuple[str, CacheType, Dict]:
    """
    last_refresh can be provided if the cache should attempt to skip insights
    whose events haven't been ingested since the last_refresh datetime
    """
    filter = get_filter(data=insight.dashboard_filters(dashboard), team=insight.team)
    cache_key = generate_cache_key("{}_{}".format(filter.toJSON(), insight.team_id))

    cache_type = get_cache_type(filter)
    payload = {
        "filter": filter.toJSON(),
        "team_id": insight.team_id,
        "insight_id": insight.id,
        "dashboard_id": None if not dashboard else dashboard.id,
        "last_refresh": last_refresh,
    }

    return cache_key, cache_type, payload
