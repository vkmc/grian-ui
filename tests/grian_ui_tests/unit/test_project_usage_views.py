#
# SPDX-License-Identifier: Apache-2.0

import csv
import datetime
import importlib
import io
import json
import re

from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.template import Context
from django.template import Engine
from django.template.loader import get_template
from django.test import RequestFactory
from django.test import override_settings
from django.urls import NoReverseMatch

from grian_ui import cache as grian_cache
from grian_ui import config
from grian_ui import project_usage as pu
from grian_ui.content.project_usage import panel
from grian_ui.content.project_usage import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_usage
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.project_usage.views"
UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 11, 20, 15, 40, tzinfo=UTC)
WEB = "d414fbd9-66a6-485c-bec7-8191c2fa4221"
BATCH = "6a0a1f43-6b0c-4f1e-9a43-3c2f1d1e8b11"
IM_URL = "/dashboard/project/instance_metrics/"
_REAL_IM_URL = views.instance_metrics_url


class _ViewTestCase(base.TestCase):
    project_id = "b8753cd7d01b4455b774b46991f36f83"

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(f"{VIEWS}.get_datasource")
        self.mock_get_ds = patcher.start()
        self.addCleanup(patcher.stop)
        # wrap the fake datasource so calls can be asserted
        self.client = mock.MagicMock(wraps=fake.get_client())
        self.mock_get_ds.return_value = self.client
        now = mock.patch(f"{VIEWS}._now", return_value=NOW)
        now.start()
        self.addCleanup(now.stop)
        servers = mock.patch(
            f"{VIEWS}.list_servers",
            side_effect=lambda request: fake_project_usage.get_servers(),
        )
        self.mock_servers = servers.start()
        self.addCleanup(servers.stop)

    def _request(self, project_id=None, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(
            services_region="RegionOne",
            project_id=project_id or self.project_id,
            project_name="demo",
        )
        return request

    def render_context(self, view_class, **params):
        view = view_class()
        view.setup(self._request(**params))
        return view.get_context_data()

    def render(self, view_class, **params):
        response = view_class.as_view()(self._request(**params))
        response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.UsageIndexView)
        self.assertEqual(context["refresh_seconds"], 300)
        self.assertEqual(context["refresh_minutes"], 5)
        self.assertEqual(context["step_minutes"], 5)
        self.assertEqual(context["retention_days"], 15)
        self.assertEqual(context["thresholds"], {"cpu_use_low": 10})
        self.assertEqual(context["default_period"], "2026-11")
        self.assertEqual(
            [(p["key"], p["available"]) for p in context["periods"]],
            [("2026-11", True), ("2026-10", False)],
        )
        self.assertEqual(context["error_messages"], [])
        # the shell queries nothing
        self.mock_get_ds.assert_not_called()
        self.mock_servers.assert_not_called()

    @override_settings(
        GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": 30}
    )
    def test_longer_retention_unlocks_october(self):
        context = self.render_context(views.UsageIndexView)
        self.assertEqual(
            [(p["key"], p["available"]) for p in context["periods"]],
            [("2026-11", True), ("2026-10", True), ("2026-09", False)],
        )

    def test_template(self):
        source = get_template(
            "project/telemetry_usage/index.html"
        ).template.source
        for name in ("summary", "chart", "table", "csv"):
            self.assertIn(
                f"{{% url 'horizon:project:telemetry_usage:{name}' %}}",
                source,
            )
        # every section sends the period and follows a period change
        self.assertEqual(source.count('hx-include="#pu-controls"'), 3)
        self.assertEqual(source.count("pu-change from:body"), 3)
        self.assertIn('name="period" id="pu-period"', source)
        self.assertIn("This is a usage report, not a bill.", source)
        # locked months: disabled, with a lock and the reason
        self.assertIn('disabled title="{{ p.locked_reason }}"', source)
        self.assertIn("fa-lock", source)
        self.assertIn('<details class="panel panel-default pu-section', source)
        self.assertNotIn("<details open", source)
        self.assertIn("GRIAN_PLUGIN['metrics_retention_days']", source)
        self.assertIn("GRIAN_PLUGIN['project_usage_thresholds']", source)
        self.assertIn('resource_name=~".+:instance-.+"', source)
        self.assertNotIn("{% url 'horizon:admin", source)

    def render_index(self, **params):
        """Render the index with stand-ins for Horizon's templates."""
        view = views.UsageIndexView()
        view.setup(self._request(**params))
        context = view.get_context_data()
        engine = Engine(
            dirs=settings.TEMPLATES[0]["DIRS"],
            loaders=[
                (
                    "django.template.loaders.locmem.Loader",
                    {
                        "base.html": "{% block main %}{% endblock %}",
                        "horizon/common/_page_header.html": "",
                    },
                ),
                "django.template.loaders.filesystem.Loader",
            ],
            libraries={
                "i18n": "django.templatetags.i18n",
                "static": "django.templatetags.static",
            },
        )
        with override_settings(STATIC_URL="/static/"):
            return engine.get_template(
                "project/telemetry_usage/index.html"
            ).render(Context(context))

    def test_render(self):
        html = self.render_index()
        self.assertIn("November 2026 (to date)", html)
        self.assertIn(
            'disabled title="Not available: older than the 15 days of '
            'metrics this cloud keeps"',
            html,
        )
        self.assertIn(
            'href="/dashboard/project/telemetry_usage/csv/?period=2026-11"',
            html,
        )

    def test_every_configuration_error_is_shown(self):
        with (
            override_settings(
                GRIAN_PLUGIN={
                    "datasource": "fake",
                    "metrics_retention_days": 0,
                    "project_usage_thresholds": {"nope": 1},
                }
            ),
        ):
            context = self.render_context(views.UsageIndexView)
        self.assertEqual(len(context["error_messages"]), 2)
        self.assertNotIn("thresholds", context)
        self.assertNotIn("periods", context)
        with override_settings(
            GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": "x"}
        ):
            html = self.render_index()
        self.assertIn('<div class="alert alert-danger">', html)
        self.assertNotIn("pu-period-buttons\"><button", html)

    def test_keystone_setting_checked_for_prometheus(self):
        with (
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=config.ConfigurationError("no keystone"),
            ),
        ):
            context = self.render_context(views.UsageIndexView)
        self.assertEqual(context["error_messages"], ["no keystone"])


class SummaryViewTests(_ViewTestCase):
    def test_cards(self):
        context = self.render_context(views.SummaryView)
        totals = context["totals"]
        self.assertEqual(totals["instances"], 3)
        self.assertTrue(context["has_usage"])
        cards = context["cards"]
        self.assertEqual(
            cards["cpu_held"], pu.format_amount(totals["vcpu_hours"])
        )
        self.assertEqual(cards["storage"], pu.format_amount(25 * 351.666666))
        self.assertFalse(context["data_stale"])
        self.assertEqual(
            context["coverage"]["range"], "11/6 00:00 – 11/20 15:40"
        )

    def test_render(self):
        html = self.render(views.SummaryView)
        self.assertIn("CPU-hours used", html)
        self.assertIn("vCPU-hours held (", html)
        self.assertIn("RAM GB-hours used", html)
        self.assertIn("Network traffic", html)
        self.assertIn("Volume GB-hours", html)
        self.assertIn("volumes +", html)
        self.assertIn('class="pu-clock"', html)
        # the coverage note next to the period buttons, swapped out of band
        self.assertIn('id="pu-coverage"', html)
        self.assertIn('hx-swap-oob="true"', html)
        self.assertIn(
            "Covers <strong>11/6 00:00 – 11/20 15:40 UTC</strong>", html
        )
        self.assertIn("(15 of 20 days)", html)

    def test_incomplete_month_warning(self):
        html = self.render(views.SummaryView)
        self.assertIn("Incomplete month: metrics are kept for 15 days.", html)
        self.assertIn(
            "11/1–11/5 and earlier months are no longer available, so these "
            "totals cover 15 of 20 days.",
            html,
        )

    def test_complete_month_info(self):
        with mock.patch(
            f"{VIEWS}._now",
            return_value=datetime.datetime(2026, 11, 10, 8, tzinfo=UTC),
        ):
            html = self.render(views.SummaryView)
        self.assertNotIn("Incomplete month", html)
        self.assertIn("(all 10 days)", html)
        self.assertIn(
            "A report run at the end of a month only covers its last 15 days.",
            html,
        )

    def test_first_usage_after_the_start(self):
        report = fake_project_usage.get_usage(
            datetime.datetime(2026, 11, 18, 9, tzinfo=UTC), NOW
        )
        self.client.get_project_usage.side_effect = None
        self.client.get_project_usage.return_value = report
        html = self.render(views.SummaryView)
        self.assertIn("this project's first usage: 11/18 09:00", html)

    def test_no_usage(self):
        self.client.get_project_usage.side_effect = None
        self.client.get_project_usage.return_value = {
            "instances": {},
            "volume_gb_hours": None,
            "snapshot_gb_hours": None,
            "hourly": {},
        }
        html = self.render(views.SummaryView)
        self.assertIn("No usage in this period", html)
        self.assertNotIn("CPU-hours used", html)

    def test_locked_period(self):
        html = self.render(views.SummaryView, period="2026-10")
        self.assertIn(
            "October 2026: Not available: older than the 15 days", html
        )
        self.assertIn("data-pu-nodata", html)
        self.client.get_project_usage.assert_not_called()

    def test_invalid_period_is_the_current_month(self):
        for period in ("2026-12", "bad", "2026-11'"):
            with self.subTest(period=period):
                context = self.render_context(views.SummaryView, period=period)
                self.assertEqual(context["period"]["key"], "2026-11")

    def test_backend_error_hides_details(self):
        self.client.get_project_usage.side_effect = RuntimeError(
            "secret backend message"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.SummaryView)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret backend message", html)
        self.assertIn("data-pu-nodata", html)
        # an error is not retried quickly
        self.assertNotIn("pu-retry-", html)
        # the coverage still follows the period
        self.assertIn('id="pu-coverage"', html)

    def test_still_computing_retries(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.SummaryView)
        self.assertIn("Still loading telemetry data", html)
        self.assertIn("pu-retry-", html)
        self.assertIn("htmx.trigger(target, 'pu-retry')", html)

    def test_configuration_error_inside_the_computation(self):
        failure = grian_cache.ComputationFailed("failed")
        failure.__cause__ = config.ConfigurationError("bad setting")
        with mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=failure):
            html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)
        self.assertNotIn("pu-retry-", html)

    def test_invalid_retention_setting(self):
        with override_settings(
            GRIAN_PLUGIN={"datasource": "fake", "metrics_retention_days": -1}
        ):
            html = self.render(views.SummaryView)
        self.assertIn("invalid configuration", html)

    def test_stale_copy(self):
        report = dict(
            fake_project_usage.get_usage(
                datetime.datetime(2026, 11, 6, tzinfo=UTC), NOW
            ),
            fetched_at=1_795_000_000,
            is_stale_copy=True,
        )
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=report):
            html = self.render(views.SummaryView)
        self.assertIn("could not be refreshed", html)

    def test_old_unmarked_copy_is_marked(self):
        report = dict(
            fake_project_usage.get_usage(
                datetime.datetime(2026, 11, 6, tzinfo=UTC), NOW
            ),
            fetched_at=1_700_000_000,
            stored_at=1_700_000_000,
        )
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=report):
            context = self.render_context(views.SummaryView)
            html = self.render(views.SummaryView)
        self.assertTrue(context["data_old"])
        self.assertIn("Refreshing: the data shown may be out of date", html)


class ChartViewTests(_ViewTestCase):
    def chart_json(self, html):
        match = re.search(
            r'<script id="pu-daily-data-[0-9a-f]+" type="application/json">'
            r"(.*?)</script>",
            html,
            re.S,
        )
        return json.loads(match.group(1))

    def test_render(self):
        html = self.render(views.ChartView)
        chart = self.chart_json(html)
        self.assertEqual(len(chart["labels"]), 20)
        self.assertEqual(
            chart["bands"],
            [{"from": 0, "to": 4, "label": "not kept: older than 15 days"}],
        )
        self.assertEqual(chart["cpu"]["held"][:5], [None] * 5)
        self.assertGreater(chart["cpu"]["held"][5], chart["cpu"]["used"][5])
        self.assertIsNone(chart["net"]["held"])
        self.assertIn('data-metric="mem"', html)
        self.assertIn("Grey: days whose metrics are no longer kept", html)
        self.assertIn("pu.replaceChart('daily'", html)
        self.assertIn("puBands", html)
        self.assertIn('role="img"', html)

    def test_no_band_in_a_complete_month(self):
        with mock.patch(
            f"{VIEWS}._now",
            return_value=datetime.datetime(2026, 11, 10, tzinfo=UTC),
        ):
            html = self.render(views.ChartView)
        self.assertEqual(self.chart_json(html)["bands"], [])
        self.assertNotIn("Grey: days whose metrics", html)

    def test_no_usage(self):
        self.client.get_project_usage.side_effect = None
        self.client.get_project_usage.return_value = {"hourly": {}}
        html = self.render(views.ChartView)
        self.assertIn("No instance of this project ran", html)

    def test_locked_period(self):
        html = self.render(views.ChartView, period="2026-10")
        self.assertIn("data-pu-nodata", html)
        self.assertIn("pu.destroyChart('daily')", html)
        self.assertNotIn("pu-daily-data-", html)

    def test_error(self):
        self.client.get_project_usage.side_effect = RuntimeError("boom")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.ChartView)
        self.assertIn("Error loading telemetry data", html)
        self.assertIn("pu.destroyChart('daily')", html)
        self.assertNotIn('data-metric="cpu"', html)

    def test_still_computing(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.ChartView)
        self.assertIn("pu-retry-", html)


class TableViewTests(_ViewTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            f"{VIEWS}.instance_metrics_url", return_value=None
        )
        self.mock_im = patcher.start()
        self.addCleanup(patcher.stop)

    def test_rows(self):
        context = self.render_context(views.TableView)
        rows = context["rows"]
        self.assertEqual(
            [r["name"] for r in rows], ["web-1", "db-1", "batch-2"]
        )
        self.assertEqual(rows[0]["flavor"], "m1.large")
        self.assertTrue(rows[2]["deleted"])
        self.assertEqual(rows[2]["flavor"], "8 vCPU, 16 GiB")
        self.assertTrue(rows[1]["cpu_low"])
        self.assertEqual(context["totals"]["instances"], 3)
        self.assertEqual(
            [(s["name"], s["status"]) for s in context["not_listed"]],
            [("build-01", "SHUTOFF"), ("grian-ih-error", "ERROR")],
        )

    def test_render(self):
        html = self.render(views.TableView)
        for column in (
            "Hours running",
            "vCPU-hours held",
            "CPU-hours used",
            "Use of held CPU",
            "RAM GB-hours held",
            "RAM GB-hours used",
            "Network in / out",
        ):
            self.assertIn(column, html)
        self.assertIn("(deleted)", html)
        self.assertIn("<td>Total</td>", html)
        self.assertIn("Sorted by vCPU-hours held.", html)
        self.assertIn(
            "<strong>build-01 (SHUTOFF), grian-ih-error (ERROR)</strong>", html
        )
        self.assertIn("they still hold quota", html)
        self.assertIn("pu-ratio pu-low", html)
        self.assertIn("instances that ran 11/6 00:00 – 11/20 15:40 UTC", html)

    def test_names_are_escaped(self):
        servers = fake_project_usage.get_servers()
        servers[0]["name"] = "<script>alert(1)</script>"
        self.mock_servers.side_effect = lambda request: servers
        html = self.render(views.TableView)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_instance_links_without_the_panel(self):
        html = self.render(views.TableView)
        self.assertNotIn("<a href", html)
        self.assertIn(f'<strong title="{WEB}">web-1</strong>', html)

    def test_instance_links_with_the_panel(self):
        self.mock_im.return_value = IM_URL
        html = self.render(views.TableView)
        self.assertIn(
            f'<a href="{IM_URL}?instance={WEB}" class="pu-link"', html
        )
        # deleted instances keep their link: their metrics exist
        self.assertIn(f"?instance={BATCH}", html)

    def test_instance_metrics_url(self):
        # setUp replaces it: test the real function, both ways
        with mock.patch(f"{VIEWS}.reverse", return_value=IM_URL) as reverse:
            self.assertEqual(_REAL_IM_URL(), IM_URL)
        reverse.assert_called_once_with(
            "horizon:project:instance_metrics:index"
        )
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(_REAL_IM_URL())

    def test_instance_link(self):
        self.assertIsNone(views.instance_link(None, WEB))
        self.assertEqual(
            views.instance_link(IM_URL, "a&b=c"),
            IM_URL + "?instance=a%26b%3Dc",
        )

    def test_compute_api_failure_is_optional(self):
        self.mock_servers.side_effect = RuntimeError("nova down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            context = self.render_context(views.TableView)
        self.assertTrue(context["servers_failed"])
        self.assertNotIn("error_message", context)
        self.assertNotIn("not_listed", context)
        # names from the metrics
        self.assertEqual(context["rows"][0]["name"], "web-1")
        self.assertIsNone(context["rows"][0]["deleted"])
        html = self.render(views.TableView)
        self.assertIn("could not be loaded from the Compute service", html)
        self.assertNotIn("(deleted)", html)

    def test_compute_api_still_loading(self):
        real = views._UsageMixin.get_or_compute

        def fake_get_or_compute(view, name, ttl, compute):
            if name == "servers":
                raise grian_cache.StillComputing("busy")
            return real(view, name, ttl, compute)

        with mock.patch.object(
            views._UsageMixin, "get_or_compute", fake_get_or_compute
        ):
            html = self.render(views.TableView)
        self.assertIn("Loading the instance list", html)
        self.assertIn("pu-retry-", html)

    def test_past_period_has_no_quota_note(self):
        with mock.patch(
            f"{VIEWS}._now",
            return_value=datetime.datetime(2026, 12, 3, 12, tzinfo=UTC),
        ):
            context = self.render_context(views.TableView, period="2026-11")
        self.assertFalse(context["period"]["is_current"])
        self.assertNotIn("not_listed", context)

    def test_no_rows(self):
        self.client.get_project_usage.side_effect = None
        self.client.get_project_usage.return_value = {"instances": {}}
        html = self.render(views.TableView)
        self.assertIn("No instance ran in this period.", html)
        self.assertNotIn("<tfoot>", html)
        # every server holds quota without running
        self.assertIn("web-1 (ACTIVE)", html)

    def test_locked_period(self):
        html = self.render(views.TableView, period="2026-10")
        self.assertIn("data-pu-nodata", html)
        self.client.get_project_usage.assert_not_called()
        self.mock_servers.assert_not_called()

    def test_error(self):
        self.client.get_project_usage.side_effect = RuntimeError("boom")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TableView)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("<table", html)

    @override_settings(
        GRIAN_PLUGIN={
            "datasource": "fake",
            "project_usage_thresholds": {"cpu_use_low": 70},
        }
    )
    def test_threshold_setting(self):
        context = self.render_context(views.TableView)
        self.assertTrue(context["rows"][0]["cpu_low"])
        html = self.render(views.TableView)
        self.assertIn("Use of held CPU under 70% is low", html)


class CacheTests(_ViewTestCase):
    def test_report_shared_between_partials(self):
        self.render_context(views.SummaryView)
        self.render_context(views.ChartView)
        self.render_context(views.TableView)
        self.render_context(views.SummaryView)
        self.assertEqual(self.client.get_project_usage.call_count, 1)

    def test_one_value_per_project(self):
        self.render_context(views.SummaryView)
        self.render_context(views.SummaryView, project_id="other-project")
        self.assertEqual(self.client.get_project_usage.call_count, 2)

    def test_keys_hold_the_project(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            wraps=grian_cache.get_or_compute,
        ) as get_or_compute:
            self.render_context(views.TableView)
        keys = [c.args[0] for c in get_or_compute.call_args_list]
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertTrue(key.startswith("grian_ui:project_usage:v2:"), key)
            self.assertTrue(key.endswith(f":project:{self.project_id}"), key)
        self.assertIn(":report_2026_11_20261106-", keys[0])
        self.assertIn(":servers:", keys[1])

    def test_no_project_no_cache(self):
        request = self._request()
        request.user.project_id = None
        view = views.SummaryView()
        view.setup(request)
        with self.assertLogs(VIEWS, "ERROR"):
            context = view.get_context_data()
        self.assertIn("Error loading telemetry data", context["error_message"])
        self.client.get_project_usage.assert_not_called()

    def test_ttl(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", return_value={"instances": {}}
        ) as get_or_compute:
            self.render_context(views.SummaryView)
            with override_settings(
                GRIAN_PLUGIN={
                    "datasource": "fake",
                    "metrics_retention_days": 60,
                }
            ):
                self.render_context(views.SummaryView, period="2026-10")
        ttls = [c.args[1] for c in get_or_compute.call_args_list]
        self.assertEqual(ttls, [views.CURRENT_CACHE_TTL, views.PAST_CACHE_TTL])

    def test_period_range_passed_to_the_datasource(self):
        self.render_context(views.SummaryView)
        _request, start, end = self.client.get_project_usage.call_args.args
        self.assertEqual(start, datetime.datetime(2026, 11, 6, tzinfo=UTC))
        self.assertEqual(end, NOW)


class CsvViewTests(_ViewTestCase):
    def get(self, **params):
        return views.CsvView.as_view()(self._request(**params))

    def test_csv(self):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertEqual(
            response["Content-Disposition"],
            'attachment; filename="usage-demo-2026-11-06_to_2026-11-20.csv"',
        )
        self.assertEqual(response["Cache-Control"], "no-store")
        records = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(records[0], list(pu.CSV_COLUMNS))
        self.assertEqual(len(records), 4)
        first = dict(zip(records[0], records[1]))
        self.assertEqual(first["period_start"], "2026-11-06T00:00:00Z")
        self.assertEqual(first["period_end"], "2026-11-20T15:40:00Z")
        self.assertEqual(first["instance"], "web-1")
        self.assertEqual(first["instance_id"], WEB)
        self.assertEqual(first["flavor"], "m1.large")
        self.assertEqual(float(first["hours_running"]), 351.67)
        self.assertEqual(float(first["vcpu_hours_held"]), 1406.67)
        self.assertTrue(first["network_in_bytes"].isdigit())
        # a deleted instance: name and size from the metrics
        self.assertEqual(records[3][2:5], ["batch-2", BATCH, "8 vCPU, 16 GiB"])

    def test_same_report_as_the_page(self):
        self.render_context(views.TableView)
        self.get()
        self.assertEqual(self.client.get_project_usage.call_count, 1)

    def test_past_period(self):
        with (
            mock.patch(
                f"{VIEWS}._now",
                return_value=datetime.datetime(2026, 12, 3, 12, tzinfo=UTC),
            ),
        ):
            response = self.get(period="2026-11")
        self.assertIn(
            "usage-demo-2026-11-19_to_2026-11-30.csv",
            response["Content-Disposition"],
        )
        records = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(
            records[1][:2], ["2026-11-19T00:00:00Z", "2026-12-01T00:00:00Z"]
        )

    def test_locked_period(self):
        with self.assertRaises(Http404):
            self.get(period="2026-10")
        self.client.get_project_usage.assert_not_called()

    def test_invalid_setting(self):
        with (
            override_settings(
                GRIAN_PLUGIN={
                    "datasource": "fake",
                    "metrics_retention_days": 0,
                }
            ),
            self.assertRaises(Http404),
        ):
            self.get()

    def test_invalid_datasource_setting(self):
        # checked before the cache: a 503 with a message, not a 500
        with (
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                side_effect=config.ConfigurationError("no keystone"),
            ),
            mock.patch(f"{VIEWS}._LOGGED_CONFIG_ERRORS", set()),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.content.decode(), "Not available: invalid configuration."
        )
        self.client.get_project_usage.assert_not_called()

    def test_data_unavailable(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute",
            side_effect=grian_cache.ComputationFailed("down"),
        ):
            response = self.get()
        self.assertEqual(response.status_code, 503)

    def test_configuration_error_inside_the_computation(self):
        # same answer as the error raised before the cache
        failure = grian_cache.ComputationFailed("failed")
        failure.__cause__ = config.ConfigurationError("bad setting")
        with (
            mock.patch(f"{VIEWS}.cache.get_or_compute", side_effect=failure),
            mock.patch(f"{VIEWS}._LOGGED_CONFIG_ERRORS", set()),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            response = self.get()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.content.decode(), "Not available: invalid configuration."
        )

    def test_without_the_compute_api(self):
        self.mock_servers.side_effect = RuntimeError("nova down")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            response = self.get()
        records = list(csv.reader(io.StringIO(response.content.decode())))
        # flavor from the metrics
        self.assertEqual(records[1][4], "4 vCPU, 8 GiB")


class ListServersTests(base.TestCase):
    def test_fake(self):
        self.assertEqual(
            views.list_servers(None), fake_project_usage.get_servers()
        )

    def _nova(self, servers, flavors=()):
        nova = mock.MagicMock()
        nova.server_list.return_value = (servers, False)
        nova.flavor_list.return_value = flavors
        api = mock.MagicMock(nova=nova)
        modules = {
            "openstack_dashboard": mock.MagicMock(api=api),
            "openstack_dashboard.api": api,
        }
        return nova, modules

    @override_settings(
        GRIAN_PLUGIN={"datasource": "prometheus"},
        OPENSTACK_KEYSTONE_URL="http://keystone/v3",
    )
    def test_compute_api(self):
        server = mock.MagicMock(
            id="i1", status="ERROR", flavor={"original_name": "m1.tiny"}
        )
        server.name = "broken"
        old = mock.MagicMock(id="i2", status="ACTIVE", flavor={"id": "f1"})
        old.name = "old"
        flavor = mock.MagicMock(id="f1")
        flavor.name = "m1.small"
        nova, modules = self._nova([server, old], [flavor])
        request = object()
        with mock.patch.dict("sys.modules", modules):
            result = views.list_servers(request)
        # the user's own token: no all_tenants
        nova.server_list.assert_called_once_with(request)
        self.assertEqual(
            result,
            [
                {
                    "id": "i1",
                    "name": "broken",
                    "status": "ERROR",
                    "flavor": "m1.tiny",
                },
                {
                    "id": "i2",
                    "name": "old",
                    "status": "ACTIVE",
                    "flavor": "m1.small",
                },
            ],
        )

    @override_settings(GRIAN_PLUGIN={"datasource": "prometheus"})
    def test_flavor_list_failure(self):
        old = mock.MagicMock(id="i2", status="ACTIVE", flavor={"id": "f1"})
        old.name = "old"
        nova, modules = self._nova([old])
        nova.flavor_list.side_effect = RuntimeError("forbidden")
        with mock.patch.dict("sys.modules", modules):
            result = views.list_servers(object())
        self.assertEqual(result[0]["flavor"], "f1")


class RetentionNotesTests(base.TestCase):
    def test_single_lost_day(self):
        now = datetime.datetime(2026, 11, 16, 12, tzinfo=UTC)
        period = pu.period_info(2026, 11, now, 15)
        self.assertEqual(views.retention_notes(period)["lost_range"], "11/1")

    def test_lost_range(self):
        period = pu.period_info(2026, 11, NOW, 15)
        self.assertEqual(
            views.retention_notes(period), {"lost_range": "11/1–11/5"}
        )

    def test_complete_month(self):
        now = datetime.datetime(2026, 11, 10, 8, tzinfo=UTC)
        period = pu.period_info(2026, 11, now, 15)
        self.assertEqual(views.retention_notes(period), {"lost_range": None})


class EnabledFileTests(base.TestCase):
    def test_enabled_file(self):
        module = importlib.import_module(
            "grian_ui.local.enabled._1104_project_telemetry_usage_panel"
        )
        self.assertEqual(module.PANEL, "telemetry_usage")
        self.assertEqual(module.PANEL_DASHBOARD, "project")
        self.assertEqual(module.PANEL_GROUP, "telemetry")
        self.assertEqual(
            module.ADD_PANEL,
            "grian_ui.content.project_usage.panel.ProjectUsage",
        )
        self.assertFalse(hasattr(module, "ADD_INSTALLED_APPS"))

    def test_panel_access(self):
        # reads Nova (servers, flavors) and the metric store
        self.assertEqual(
            panel.ProjectUsage.permissions,
            (
                "openstack.services.compute",
                "openstack.services.metric-storage",
            ),
        )
        self.assertEqual(
            panel.ProjectUsage.policy_rules,
            (("telemetry", "telemetry:query"),),
        )
