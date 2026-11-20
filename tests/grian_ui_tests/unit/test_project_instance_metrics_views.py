#
# SPDX-License-Identifier: Apache-2.0

import importlib
import json
import re
import sys
import types

from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.template.loader import get_template
from django.test import RequestFactory
from django.test import override_settings
from django.urls import NoReverseMatch
from django.urls import reverse

from grian_ui import cache as grian_cache
from grian_ui import config
from grian_ui import project_instance_metrics as pim
from grian_ui.content.project_instance_metrics import views
from grian_ui.datasource import fake
from grian_ui.datasource import fake_project_instance_metrics as fake_data
from grian_ui_tests.unit import base


VIEWS = "grian_ui.content.project_instance_metrics.views"
PROJECT = "b8753cd7d01b4455b774b46991f36f83"
OTHER_PROJECT = "7bb2692e0d994decacab9a372f166676"
WEB = "5d0c3f6e-1b2a-4c8d-9e7f-0a1b2c3d4e5f"
DB = "8f4e2d1c-3b5a-4d6e-8f7a-1b2c3d4e5f60"
SHUTOFF = "2a3b4c5d-6e7f-4a8b-9c0d-1e2f3a4b5c6d"
ERROR = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
UNKNOWN = "00000000-0000-4000-8000-000000000000"
EVIL = "</script><script>alert(1)</script>"

# Renders index.html without horizon: a stand-in for its base.html.
_BASE = (
    "<title>{% block title %}{% endblock %}</title>"
    "{% block page_header %}{% endblock %}{% block main %}{% endblock %}"
)
INDEX_TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": settings.TEMPLATES[0]["DIRS"],
        "OPTIONS": {
            "loaders": [
                (
                    "django.template.loaders.locmem.Loader",
                    {"base.html": _BASE},
                ),
                "django.template.loaders.filesystem.Loader",
            ]
        },
    }
]


def _server(**changes):
    """A server as fetch_server returns it, in the test project."""
    server = dict(fake_data.server(PROJECT, DB))
    server.update(changes)
    return server


class _ViewTestCase(base.TestCase):
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

    def _request(self, project=PROJECT, **params):
        request = RequestFactory().get("/", params)
        request.user = mock.MagicMock(
            services_region="RegionOne", project_id=project
        )
        return request

    def render_context(self, view_class, project=PROJECT, kwargs=None, **p):
        view = view_class()
        view.setup(self._request(project, **p), **(kwargs or {}))
        return view.get_context_data(**(kwargs or {}))

    def render(self, view_class, project=PROJECT, kwargs=None, **params):
        response = view_class.as_view()(
            self._request(project, **params), **(kwargs or {})
        )
        response.render()
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def charts(self, group="cpu", **params):
        return self.render(views.ChartsView, kwargs={"group": group}, **params)

    def chart_data(self, html):
        found = re.search(
            r'<script id="im-data-[0-9a-f]+" type="application/json">'
            r"(.*?)</script>",
            html,
            re.S,
        )
        self.assertIsNotNone(found)
        return json.loads(found.group(1))


class IndexViewTests(_ViewTestCase):
    def test_context(self):
        context = self.render_context(views.IndexView, instance=DB)
        self.assertEqual(context["instance_id"], DB)
        self.assertEqual(context["server"]["name"], "db-01")
        self.assertEqual(context["ranges"], ["1h", "6h", "24h", "7d"])
        self.assertEqual(context["range"], "24h")
        self.assertEqual(context["refresh_seconds"], 30)
        self.assertEqual(context["groups"], pim.GROUPS)
        self.assertEqual(
            context["range_steps"],
            [
                "1h: every 1m, rates over 10m",
                "6h: every 5m, rates over 10m",
                "24h: every 15m, rates over 15m",
                "7d: every 1h, rates over 1h",
            ],
        )
        self.assertEqual(context["error_messages"], [])
        self.assertTrue(context["picked"])
        # the picker lists the project's instances by name
        self.assertEqual(
            [s["name"] for s in context["servers"]],
            ["build-01", "db-01", "old-test", "web-01"],
        )
        # the shell does not query the metrics
        self.client.get_project_instance_metrics.assert_not_called()

    def test_range_parameter(self):
        context = self.render_context(views.IndexView, instance=DB, range="1h")
        self.assertEqual(context["range"], "1h")
        context = self.render_context(views.IndexView, instance=DB, range="9y")
        self.assertEqual(context["range"], "24h")

    def test_retention_limits_the_ranges(self):
        plugin = {"datasource": "fake", "metrics_retention_days": 2}
        with override_settings(GRIAN_PLUGIN=plugin):
            context = self.render_context(
                views.IndexView, instance=DB, range="7d"
            )
        self.assertEqual(context["ranges"], ["1h", "6h", "24h"])
        self.assertEqual(context["range"], "24h")
        self.assertEqual(context["metrics_retention_days"], 2)

    def test_without_instance_shows_the_picker(self):
        context = self.render_context(views.IndexView)
        self.assertIsNone(context["instance_id"])
        self.assertNotIn("server", context)
        self.assertFalse(context["picked"])
        self.assertEqual(len(context["servers"]), 4)

    def test_invalid_instance(self):
        with mock.patch(f"{VIEWS}.fetch_server") as fetch:
            context = self.render_context(views.IndexView, instance="1 or 1=1")
        self.assertTrue(context["invalid"])
        self.assertIsNone(context["instance_id"])
        fetch.assert_not_called()

    def test_unknown_instance(self):
        context = self.render_context(views.IndexView, instance=UNKNOWN)
        self.assertTrue(context["not_found"])
        self.assertNotIn("server", context)

    def test_another_projects_instance_is_refused(self):
        # what the Compute API returns to an administrator
        foreign = _server(project_id=OTHER_PROJECT)
        with (
            mock.patch(f"{VIEWS}.fetch_server", return_value=foreign),
            self.assertLogs(VIEWS, "WARNING") as logs,
        ):
            context = self.render_context(views.IndexView, instance=DB)
        self.assertTrue(context["not_found"])
        self.assertNotIn("server", context)
        self.assertIn("another project", logs.output[0])

    def test_nova_error(self):
        with (
            mock.patch(
                f"{VIEWS}.fetch_server", side_effect=RuntimeError("secret")
            ),
            self.assertLogs("grian_ui.cache", "ERROR"),
        ):
            context = self.render_context(views.IndexView, instance=DB)
        self.assertIn("Error loading", str(context["error_message"]))
        self.assertNotIn("not_found", context)

    def test_still_computing_renders_the_shell(self):
        with mock.patch.object(
            views.IndexView,
            "get_server",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            context = self.render_context(views.IndexView, instance=DB)
        self.assertIsNone(context["server"])
        self.assertNotIn("error_message", context)

    def test_picker_error(self):
        with (
            mock.patch(
                f"{VIEWS}.list_servers", side_effect=RuntimeError("nova")
            ),
            self.assertLogs("grian_ui.cache", "ERROR"),
        ):
            context = self.render_context(views.IndexView, instance=DB)
        self.assertTrue(context["picker_error"])
        self.assertEqual(context["servers"], [])
        # the instance itself still loads
        self.assertEqual(context["server"]["name"], "db-01")

    def test_picker_limit(self):
        many = [
            {"id": f"{i:08x}-0000-4000-8000-000000000000", "name": f"vm-{i}"}
            for i in range(views.PICKER_LIMIT + 3)
        ]
        many = [dict(s, status="ACTIVE") for s in many]
        with mock.patch(f"{VIEWS}.list_servers", return_value=many):
            context = self.render_context(views.IndexView)
        self.assertEqual(len(context["servers"]), views.PICKER_LIMIT)
        self.assertEqual(context["picker_more"], 3)

    def test_every_configuration_error_is_shown(self):
        plugin = {
            "datasource": "fake",
            "metrics_retention_days": 0,
            "project_instance_metrics_rate_window": "1d",
            "project_instance_metrics_thresholds": {"nope": 1},
        }
        with override_settings(GRIAN_PLUGIN=plugin):
            context = self.render_context(views.IndexView, instance=DB)
        self.assertEqual(len(context["error_messages"]), 3)
        self.assertNotIn("server", context)
        self.assertNotIn("ranges", context)

    def test_keystone_url_is_checked_with_prometheus(self):
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
            context = self.render_context(views.IndexView, instance=DB)
        self.assertEqual(context["error_messages"], ["no keystone"])

    def test_template(self):
        source = get_template(
            "project/instance_metrics/index.html"
        ).template.source
        for name in ("info", "tiles"):
            self.assertIn(
                f"{{% url 'horizon:project:instance_metrics:{name}' %}}",
                source,
            )
        self.assertIn(
            "{% url 'horizon:project:instance_metrics:charts' group %}", source
        )
        self.assertIn('hx-include="#im-controls"', source)
        self.assertIn("im-change from:body", source)
        self.assertIn("im-retry", source)
        # browser-local times
        self.assertIn("toLocaleTimeString", source)
        # a native disclosure, collapsed by default
        self.assertIn('<details class="panel panel-default im-section', source)
        self.assertNotIn("<details open", source)
        self.assertIn('resource_name=~".+:instance-.+"', source)
        self.assertIn(
            "GRIAN_PLUGIN['project_instance_metrics_thresholds']", source
        )


@override_settings(TEMPLATES=INDEX_TEMPLATES, STATIC_URL="/static/")
class IndexRenderTests(_ViewTestCase):
    def test_render(self):
        # without the Compute page (another change): no breadcrumb link
        with mock.patch(f"{VIEWS}.compute_url", return_value=None):
            html = self.render(views.IndexView, instance=DB, range="6h")
        self.assertIn("<title>db-01 – Instance metrics</title>", html)
        for name in ("info", "tiles", "charts/cpu", "charts/network"):
            self.assertIn(
                f'hx-get="/dashboard/project/instance_metrics/{name}/"', html
            )
        self.assertIn(f'name="instance" value="{DB}"', html)
        self.assertIn('id="im-range" value="6h"', html)
        self.assertIn('data-range="6h" aria-pressed="true"', html)
        self.assertIn(f'<option value="{DB}" selected>db-01</option>', html)
        self.assertIn(
            f'<option value="{ERROR}">old-test (ERROR)</option>', html
        )
        self.assertIn(
            '<input type="hidden" name="range" id="im-picker-range"', html
        )
        # compute_url patched to None above: no breadcrumb link
        self.assertNotIn("Compute</a>", html)

    def test_picker_names_are_escaped(self):
        servers = [{"id": DB, "name": EVIL, "status": "ACTIVE"}]
        with (
            mock.patch(f"{VIEWS}.list_servers", return_value=servers),
            mock.patch(
                f"{VIEWS}.fetch_server", return_value=_server(name=EVIL)
            ),
        ):
            html = self.render(views.IndexView, instance=DB)
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("&lt;/script&gt;&lt;script&gt;alert(1)", html)

    def test_choose_an_instance(self):
        html = self.render(views.IndexView)
        self.assertIn("Choose an instance", html)
        self.assertIn('<option value="" selected>', html)
        self.assertNotIn("hx-get", html)

    def test_no_instances(self):
        with mock.patch(f"{VIEWS}.list_servers", return_value=[]):
            html = self.render(views.IndexView)
        self.assertIn("No instances in this project", html)
        self.assertNotIn('id="im-picker"', html)

    def test_invalid(self):
        html = self.render(views.IndexView, instance=EVIL)
        self.assertIn("Invalid instance ID", html)
        self.assertNotIn("hx-get", html)
        self.assertNotIn("<script>alert(1)", html)

    def test_foreign_instance(self):
        foreign = _server(project_id=OTHER_PROJECT, name="secret-vm")
        with (
            mock.patch(f"{VIEWS}.fetch_server", return_value=foreign),
            self.assertLogs(VIEWS, "WARNING"),
        ):
            html = self.render(views.IndexView, instance=DB)
        self.assertIn("not in the current project", html)
        self.assertNotIn("secret-vm", html)
        self.assertNotIn("hx-get", html)

    def test_compute_breadcrumb(self):
        with mock.patch(f"{VIEWS}.compute_url", return_value="/compute/"):
            html = self.render(views.IndexView, instance=DB)
        self.assertIn('<a href="/compute/">Compute</a>', html)


class InfoViewTests(_ViewTestCase):
    def test_active(self):
        html = self.render(views.InfoView, instance=DB)
        self.assertIn("ACTIVE", html)
        self.assertIn(
            "<strong>m1.medium</strong> | 2 vCPUs | 4 GiB RAM | 40 GB disk",
            html,
        )
        self.assertIn(f"<code>{DB}</code>", html)
        self.assertIn("backend | 10.0.1.20, 203.0.113.21", html)
        self.assertNotIn("No metrics for this instance", html)
        # no Horizon instance pages in the unit tests: no buttons
        self.assertNotIn("Instance details", html)
        self.client.get_project_instance_metrics.assert_not_called()

    def test_links(self):
        links = {
            "detail": "/project/instances/x/",
            "console": "/project/instances/x/?tab=instance_details__console",
            "resize": "/project/instances/x/resize",
        }
        with mock.patch(f"{VIEWS}.instance_links", return_value=links):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn('href="/project/instances/x/"', html)
        self.assertIn("instance_details__console", html)
        self.assertIn('class="btn btn-sm btn-default ajax-modal"', html)

    def test_created_inside_the_range(self):
        now = 1_800_000_000
        server = _server(created_at=now - 9 * 3600 - 100)
        with (
            mock.patch(f"{VIEWS}.fetch_server", return_value=server),
            mock.patch(f"{VIEWS}.time.time", return_value=now),
        ):
            html = self.render(views.InfoView, instance=DB, range="24h")
            self.assertIn("hold at most 9 hours of data", html)
            self.assertIn(f'data-ts="{now - 9 * 3600 - 100}"', html)
            cache.clear()
            html = self.render(views.InfoView, instance=DB, range="6h")
            self.assertNotIn("hold at most", html)

    def test_error_state(self):
        html = self.render(views.InfoView, instance=ERROR)
        self.assertIn('data-im-state="error"', html)
        self.assertIn("No metrics for this instance", html)
        self.assertIn("<em>No valid host was found.</em>", html)
        self.assertIn(
            "It still counts against your quota: 2 vCPUs and 4 GiB RAM.", html
        )
        self.assertIn("im.destroyCharts('')", html)

    def test_shutoff_state(self):
        html = self.render(views.InfoView, instance=SHUTOFF)
        self.assertIn('data-im-state="shutoff"', html)
        self.assertIn(
            "It is SHUTOFF. Metrics stop when an instance stops; the history "
            "from when it ran is kept for 15 days.",
            html,
        )
        # the Console button only makes sense while it runs
        links = dict.fromkeys(("detail", "console", "resize"), "/x/")
        with mock.patch(f"{VIEWS}.instance_links", return_value=links):
            html = self.render(views.InfoView, instance=SHUTOFF)
        self.assertNotIn("Console", html)

    def test_other_state(self):
        server = _server(status="BUILD")
        with mock.patch(f"{VIEWS}.fetch_server", return_value=server):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn('data-im-state="other"', html)
        self.assertIn("Its status is BUILD", html)

    def test_compute_page_link_only_when_installed(self):
        with mock.patch(f"{VIEWS}.compute_url", return_value=None):
            html = self.render(views.InfoView, instance=ERROR)
        self.assertNotIn("Holding quota without running", html)
        with mock.patch(f"{VIEWS}.compute_url", return_value="/compute/"):
            html = self.render(views.InfoView, instance=ERROR)
        self.assertIn('<a href="/compute/">', html)
        self.assertIn("Holding quota without running", html)

    def test_unknown_flavor(self):
        server = _server(
            flavor={
                "name": None,
                "vcpus": None,
                "ram_mib": None,
                "disk_gb": None,
            }
        )
        with mock.patch(f"{VIEWS}.fetch_server", return_value=server):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn("<strong>unknown</strong></td>", html)

    def test_invalid_or_missing_instance(self):
        with mock.patch(f"{VIEWS}.fetch_server") as fetch:
            for params in ({}, {"instance": "nope"}, {"instance": EVIL}):
                html = self.render(views.InfoView, **params)
                self.assertIn("not in the current project", html)
                self.assertNotIn("<script>alert", html)
        fetch.assert_not_called()

    def test_foreign_instance(self):
        foreign = _server(project_id=OTHER_PROJECT, name="secret-vm")
        with (
            mock.patch(f"{VIEWS}.fetch_server", return_value=foreign),
            self.assertLogs(VIEWS, "WARNING"),
        ):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn("not in the current project", html)
        self.assertNotIn("secret-vm", html)
        self.assertNotIn("tc-retry", html)

    def test_stale_server(self):
        value = {
            "server": _server(),
            "is_stale_copy": True,
            "stored_at": 1,
        }
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=value):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn("showing the last status received", html)


class TilesViewTests(_ViewTestCase):
    def test_render(self):
        html = self.render(views.TilesView, instance=DB)
        for key in ("cpu", "cpu_wait", "memory", "disk", "network"):
            self.assertIn(f'data-im-tile="{key}"', html)
        self.assertIn("<small>of 2 vCPU</small>", html)
        self.assertIn('class="im-clock"', html)
        self.assertIn("Data from", html)

    def test_reads_the_last_hour_whatever_the_range(self):
        self.render(views.TilesView, instance=DB, range="7d")
        ranges = {
            c.args[3]
            for c in self.client.get_project_instance_metrics.call_args_list
        }
        self.assertEqual(ranges, {"1h"})
        self.assertEqual(
            self.client.get_project_instance_metrics.call_count, 4
        )

    def test_levels(self):
        context = self.render_context(views.TilesView, instance=DB)
        levels = {t["key"]: t["level"] for t in context["tiles"]}
        self.assertEqual(levels["cpu"], "")
        html = self.render(views.TilesView, instance=DB)
        self.assertNotIn("text-danger", html)

    def test_danger_tile_has_text(self):
        busy = {
            "cpu": [[1, 3.9]],
            "cpu_wait": [[1, 1.0]],
            "vcpus": [[1, 4]],
        }

        def fake_metrics(matcher, group, *args, **kwargs):
            if group == "cpu":
                return busy
            return {name: [] for name in pim.SERIES[group]}

        self.client.get_project_instance_metrics.side_effect = fake_metrics
        with mock.patch(f"{VIEWS}.timeseries.gap_filled_series") as filled:
            filled.side_effect = lambda points, r, at: points
            html = self.render(views.TilesView, instance=DB)
        self.assertIn("text-danger", html)
        self.assertIn("(critical)", html)

    def test_not_running(self):
        for instance in (SHUTOFF, ERROR):
            html = self.render(views.TilesView, instance=instance)
            self.assertNotIn("im-tile", html)
            self.assertNotIn("alert", html)
        self.client.get_project_instance_metrics.assert_not_called()

    def test_no_metrics(self):
        self.client.get_project_instance_metrics.side_effect = (
            lambda m, group, *a, **k: {n: [] for n in pim.SERIES[group]}
        )
        html = self.render(views.TilesView, instance=DB)
        self.assertIn("No metrics for this instance in the last 1h", html)
        self.assertIn("data-im-nodata", html)

    def test_one_group_fails(self):
        real = fake.get_client()

        def flaky(matcher, group, *args, **kwargs):
            if group == "disk":
                raise RuntimeError("secret backend message")
            return real.get_project_instance_metrics(
                matcher, group, *args, **kwargs
            )

        self.client.get_project_instance_metrics.side_effect = flaky
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TilesView, instance=DB)
        self.assertIn('data-im-tile="cpu"', html)
        self.assertNotIn('data-im-tile="disk"', html)
        self.assertIn("Some values could not be loaded", html)
        self.assertNotIn("secret backend message", html)

    def test_backend_error_hides_details(self):
        self.client.get_project_instance_metrics.side_effect = RuntimeError(
            "secret backend message"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.TilesView, instance=DB)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret backend message", html)
        self.assertIn("data-im-nodata", html)
        # an error is not retried quickly
        self.assertNotIn("im-retry-", html)

    def test_still_computing_retries(self):
        with mock.patch.object(
            views.TilesView,
            "get_series",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.render(views.TilesView, instance=DB)
        self.assertIn("Still loading telemetry data", html)
        self.assertIn("im-retry-", html)
        self.assertIn("htmx.trigger(target, 'im-retry')", html)

    def test_stale_copy(self):
        real = views.TilesView.get_series

        def stale(view, group, instance_id, range_key):
            series, value = real(view, group, instance_id, range_key)
            return series, dict(value, is_stale_copy=True)

        with mock.patch.object(views.TilesView, "get_series", stale):
            html = self.render(views.TilesView, instance=DB)
        self.assertIn("could not be refreshed", html)

    def test_old_copy_is_marked(self):
        value = {
            "series": {n: [] for n in pim.SERIES["cpu"]},
            "fetched_at": 1_700_000_000,
            "stored_at": 1_700_000_000,
        }
        view = views.TilesView()
        view.setup(self._request(instance=DB))
        with mock.patch(f"{VIEWS}.cache.get_or_compute", return_value=value):
            found = view.get_or_compute("x", 25, dict)
        self.assertTrue(found["is_old_copy"])

    def test_not_found(self):
        html = self.render(views.TilesView, instance=UNKNOWN)
        # said once, by the instance header
        self.assertNotIn("alert", html)
        self.assertNotIn("im-tile", html)


class ChartsViewTests(_ViewTestCase):
    def test_render_every_group(self):
        expected = {
            "cpu": ["cpu", "cpu_wait"],
            "memory": ["memory"],
            "disk": ["disk", "disk_latency"],
            "network": ["network"],
        }
        for group, keys in expected.items():
            with self.subTest(group=group):
                html = self.charts(group, instance=DB)
                data = self.chart_data(html)
                self.assertEqual([c["key"] for c in data["charts"]], keys)
                self.assertEqual(data["range"], "24h")
                self.assertEqual(len(data["timestamps"]), 96)
                self.assertEqual(data["instance"], "db-01")
                self.assertIn(f"im.drawGroup('{group}', '", html)
                for key in keys:
                    self.assertRegex(
                        html, rf'<canvas id="im-canvas-[0-9a-f]{{12}}-{key}"'
                    )

    def test_canvas_ids_are_unique_per_response(self):
        # htmx settles same-id canvases with the old attributes, which
        # resets their size after the chart is drawn
        first = re.findall(r'<canvas id="([^"]+)"', self.charts(instance=DB))
        second = re.findall(r'<canvas id="([^"]+)"', self.charts(instance=DB))
        self.assertEqual(len(first), 2)
        self.assertFalse(set(first) & set(second))

    def test_instance_name_is_not_script(self):
        server = _server(name=EVIL)
        with mock.patch(f"{VIEWS}.fetch_server", return_value=server):
            html = self.charts(instance=DB)
        self.assertNotIn(EVIL, html)
        self.assertNotIn("<script>alert(1)", html)
        # json_script escapes <, > and & inside the JSON
        self.assertIn("\\u003C/script\\u003E\\u003Cscript\\u003E", html)
        self.assertEqual(self.chart_data(html)["instance"], EVIL)

    def test_range_switching(self):
        html = self.charts(instance=DB, range="1h")
        self.assertEqual(len(self.chart_data(html)["timestamps"]), 60)
        args = self.client.get_project_instance_metrics.call_args
        self.assertEqual(args.args[1:5], ("cpu", DB, "1h", "10m"))
        self.assertEqual(args.args[0], f'project="{PROJECT}"')
        html = self.charts(instance=DB, range="bogus")
        self.assertEqual(self.chart_data(html)["range"], "24h")

    def test_unknown_group_falls_back(self):
        context = self.render_context(
            views.ChartsView, kwargs={"group": "gpu"}, instance=DB
        )
        self.assertEqual(context["group"], "cpu")

    def test_no_data(self):
        self.client.get_project_instance_metrics.side_effect = (
            lambda m, group, *a, **k: {n: [] for n in pim.SERIES[group]}
        )
        html = self.charts("network", instance=DB)
        self.assertIn("No data in the last 24h", html)
        self.assertNotIn("<canvas", html)

    def test_too_few_writes(self):
        self.client.get_project_instance_metrics.side_effect = (
            lambda *a, **k: {
                "disk_read": [],
                "disk_write": [[1, 100.0]],
                "disk_write_latency": [[1, 31.0]],
            }
        )
        with mock.patch(f"{VIEWS}.timeseries.gap_filled_series") as filled:
            filled.side_effect = lambda points, r, at: points
            html = self.charts("disk", instance=DB)
        self.assertIn("Too few writes in the last 24h", html)

    def test_error(self):
        self.client.get_project_instance_metrics.side_effect = RuntimeError(
            "secret"
        )
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.charts("memory", instance=DB)
        self.assertIn("Error loading telemetry data", html)
        self.assertIn("<h3 class=\"panel-title\">Memory</h3>", html)
        self.assertIn("im.destroyCharts('memory:')", html)
        self.assertNotIn("secret", html)

    def test_still_computing(self):
        with mock.patch.object(
            views.ChartsView,
            "get_series",
            side_effect=grian_cache.StillComputing("busy"),
        ):
            html = self.charts(instance=DB)
        self.assertIn("Still loading", html)
        self.assertIn("im-retry-", html)

    def test_not_running_or_not_found(self):
        for instance in (SHUTOFF, ERROR, UNKNOWN):
            html = self.charts("disk", instance=instance)
            self.assertIn("im.destroyCharts('disk:')", html)
            self.assertNotIn("<canvas", html)
            self.assertNotIn("alert", html)
        self.client.get_project_instance_metrics.assert_not_called()

    def test_invalid_configuration(self):
        plugin = {"datasource": "fake", "metrics_retention_days": "x"}
        with (
            override_settings(GRIAN_PLUGIN=plugin),
            self.assertLogs(VIEWS, "ERROR"),
        ):
            views._LOGGED_CONFIG_ERRORS.clear()
            html = self.charts(instance=DB)
        self.assertIn("invalid configuration", html)

    def test_configuration_error_inside_a_computation(self):
        exc = grian_cache.ComputationFailed("failed")
        exc.__cause__ = config.ConfigurationError("bad")
        context = {}
        views._InstanceMetricsMixin().handle_error(context, exc)
        self.assertIn("invalid configuration", str(context["error_message"]))


class CacheTests(_ViewTestCase):
    """Values are shared per project and instance, never across projects."""

    def test_series_are_shared_by_the_partials_and_members(self):
        self.charts("cpu", instance=DB)
        self.charts("cpu", instance=DB)
        self.render(views.TilesView, instance=DB, range="1h")
        self.charts("cpu", instance=DB, range="1h")
        calls = self.client.get_project_instance_metrics.call_args_list
        # cpu over 24h, then every group over 1h (shared with the chart)
        self.assertEqual(
            [(c.args[1], c.args[3]) for c in calls],
            [
                ("cpu", "24h"),
                ("cpu", "1h"),
                ("memory", "1h"),
                ("disk", "1h"),
                ("network", "1h"),
            ],
        )

    def test_server_is_read_once_for_every_partial(self):
        with mock.patch(
            f"{VIEWS}.fetch_server", side_effect=lambda r, i: _server()
        ) as fetch:
            self.render(views.InfoView, instance=DB)
            self.render(views.TilesView, instance=DB)
            for group in pim.GROUPS:
                self.charts(group, instance=DB)
        self.assertEqual(fetch.call_count, 1)

    def test_key_per_project(self):
        self.charts(instance=DB)
        self.charts(instance=DB, project=OTHER_PROJECT)
        calls = self.client.get_project_instance_metrics.call_args_list
        self.assertEqual(
            [c.args[0] for c in calls],
            [f'project="{PROJECT}"', f'project="{OTHER_PROJECT}"'],
        )

    def test_key_per_instance(self):
        self.charts(instance=DB)
        self.charts(instance=WEB)
        calls = self.client.get_project_instance_metrics.call_args_list
        self.assertEqual([c.args[2] for c in calls], [DB, WEB])

    def test_keys(self):
        with mock.patch(
            f"{VIEWS}.cache.get_or_compute", wraps=grian_cache.get_or_compute
        ) as spy:
            self.charts("disk", instance=DB, range="6h")
        keys = [c.args[0] for c in spy.call_args_list]
        self.assertEqual(len(keys), 2)
        for key in keys:
            self.assertTrue(
                key.startswith("grian_ui:project_instance_metrics:v1:")
            )
            self.assertTrue(key.endswith(f":project:{PROJECT}"), key)
        # readable, and the instance in each (hashed past 40 characters)
        self.assertIn(":server_8f4e2d1c_3b5a_", keys[0])
        self.assertIn(":series_disk_6h_8f4e2d1c_", keys[1])

    def test_without_a_project(self):
        with self.assertLogs(VIEWS, "ERROR"):
            html = self.charts(instance=DB, project=None)
        self.assertIn("Error loading telemetry data", html)
        self.client.get_project_instance_metrics.assert_not_called()

    def test_foreign_server_cached_for_one_project_is_refused_for_it(self):
        foreign = _server(project_id=OTHER_PROJECT)
        with (
            mock.patch(f"{VIEWS}.fetch_server", return_value=foreign),
            self.assertLogs(VIEWS, "WARNING"),
        ):
            for _i in range(2):
                html = self.charts(instance=DB)
                self.assertNotIn("<canvas", html)
        self.client.get_project_instance_metrics.assert_not_called()


def _nova_modules(nova):
    api = mock.MagicMock(nova=nova)
    return {
        "openstack_dashboard": mock.MagicMock(api=api),
        "openstack_dashboard.api": api,
        "openstack_dashboard.api.nova": nova,
    }


class _NotFound(Exception):
    code = 404


def _nova_server(**changes):
    server = types.SimpleNamespace(
        id=DB,
        name="grian-ih-disk",
        status="ACTIVE",
        tenant_id=PROJECT,
        flavor={
            "original_name": "m1.tiny",
            "vcpus": 1,
            "ram": 512,
            "disk": 1,
        },
        addresses={"private": [{"addr": "10.0.0.54", "version": 4}]},
        created="2026-11-18T09:59:18Z",
        fault=None,
    )
    for key, value in changes.items():
        setattr(server, key, value)
    return server


class NovaTests(_ViewTestCase):
    """The Compute API path (datasource "prometheus")."""

    def setUp(self):
        super().setUp()
        self.nova = mock.MagicMock()
        for patcher in (
            mock.patch.dict(sys.modules, _nova_modules(self.nova)),
            mock.patch(
                f"{VIEWS}.config.get_datasource_type",
                return_value=config.DATASOURCE_PROMETHEUS,
            ),
            mock.patch(
                f"{VIEWS}.config.get_openstack_keystone_url",
                return_value="http://keystone",
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fetch_server_with_embedded_flavor(self):
        self.nova.server_get.return_value = _nova_server()
        request = self._request()
        server = views.fetch_server(request, DB)
        self.nova.server_get.assert_called_once_with(request, DB)
        self.nova.flavor_get.assert_not_called()
        self.assertEqual(
            server,
            {
                "id": DB,
                "name": "grian-ih-disk",
                "status": "ACTIVE",
                "project_id": PROJECT,
                "flavor": {
                    "name": "m1.tiny",
                    "vcpus": 1,
                    "ram_mib": 512,
                    "disk_gb": 1,
                },
                "addresses": [{"network": "private", "ips": ["10.0.0.54"]}],
                "created_at": 1794995958.0,
                "fault": None,
            },
        )

    def test_fetch_server_reads_the_flavor(self):
        self.nova.server_get.return_value = _nova_server(
            flavor={"id": "42"}, status="ERROR", fault={"message": "No host"}
        )
        flavor = types.SimpleNamespace(name="big", vcpus=8, ram=40960, disk=80)
        self.nova.flavor_get.return_value = flavor
        server = views.fetch_server(self._request(), DB)
        self.assertEqual(server["flavor"]["name"], "big")
        self.assertEqual(server["flavor"]["vcpus"], 8)
        self.assertEqual(server["fault"], "No host")

    def test_flavor_that_cannot_be_read(self):
        self.nova.server_get.return_value = _nova_server(flavor={"id": "42"})
        self.nova.flavor_get.side_effect = RuntimeError("deleted")
        with self.assertLogs(VIEWS, "INFO"):
            server = views.fetch_server(self._request(), DB)
        self.assertEqual(server["flavor"]["name"], "42")
        self.assertIsNone(server["flavor"]["vcpus"])

    def test_not_found(self):
        self.nova.server_get.side_effect = _NotFound("gone")
        self.assertIsNone(views.fetch_server(self._request(), DB))
        html = self.render(views.InfoView, instance=DB)
        self.assertIn("not in the current project", html)

    def test_compute_error(self):
        self.nova.server_get.side_effect = RuntimeError("secret")
        with self.assertLogs("grian_ui.cache", "ERROR"):
            html = self.render(views.InfoView, instance=DB)
        self.assertIn("Error loading telemetry data", html)
        self.assertNotIn("secret", html)

    def test_admin_cannot_open_another_projects_instance(self):
        # Nova returns any instance to an administrator, whatever project
        # the token is scoped to
        self.nova.server_get.return_value = _nova_server(
            tenant_id=OTHER_PROJECT, name="secret-vm"
        )
        with self.assertLogs(VIEWS, "WARNING"):
            for view, kwargs in (
                (views.InfoView, None),
                (views.TilesView, None),
                (views.ChartsView, {"group": "cpu"}),
            ):
                html = self.render(view, kwargs=kwargs, instance=DB)
                self.assertNotIn("secret-vm", html)
                self.assertNotIn("im-tile", html)
                self.assertNotIn("<canvas", html)
        self.client.get_project_instance_metrics.assert_not_called()

    def test_list_servers(self):
        servers = [
            types.SimpleNamespace(id="b", name="Zeta", status="ACTIVE"),
            types.SimpleNamespace(id="a", name="alpha", status="ERROR"),
            types.SimpleNamespace(id="c", name="", status="ACTIVE"),
        ]
        self.nova.server_list.return_value = (servers, False)
        request = self._request()
        self.assertEqual(
            views.list_servers(request),
            [
                {"id": "a", "name": "alpha", "status": "ERROR"},
                {"id": "c", "name": "c", "status": "ACTIVE"},
                {"id": "b", "name": "Zeta", "status": "ACTIVE"},
            ],
        )
        self.nova.server_list.assert_called_once_with(request)

    def test_not_found_detection(self):
        self.assertTrue(views._is_not_found(_NotFound()))
        self.assertTrue(
            views._is_not_found(mock.Mock(code=None, status_code=404))
        )
        self.assertFalse(views._is_not_found(RuntimeError("x")))


class LinkTests(base.TestCase):
    def test_instance_links(self):
        with mock.patch(
            f"{VIEWS}.reverse",
            side_effect=lambda name, args: f"/{name}/{args[0]}",
        ):
            links = views.instance_links(DB)
        self.assertEqual(
            links["detail"], f"/horizon:project:instances:detail/{DB}"
        )
        self.assertEqual(
            links["console"],
            f"/horizon:project:instances:detail/{DB}"
            "?tab=instance_details__console",
        )
        self.assertEqual(
            links["resize"], f"/horizon:project:instances:resize/{DB}"
        )

    def test_without_horizon_pages(self):
        self.assertEqual(
            views.instance_links(DB),
            {"detail": None, "console": None, "resize": None},
        )

    def test_compute_url(self):
        with mock.patch(f"{VIEWS}.reverse", return_value="/c/") as rev:
            self.assertEqual(views.compute_url(), "/c/")
        rev.assert_called_once_with(
            "horizon:project:telemetry_compute:index", args=()
        )
        with mock.patch(
            f"{VIEWS}.reverse", side_effect=NoReverseMatch("not installed")
        ):
            self.assertIsNone(views.compute_url())


class PanelTests(base.TestCase):
    def test_detail_page_contract(self):
        # other panels link here with ?instance=<server uuid>
        self.assertEqual(
            reverse("horizon:project:instance_metrics:index"),
            "/dashboard/project/instance_metrics/",
        )
        for group in pim.GROUPS:
            self.assertEqual(
                reverse(
                    "horizon:project:instance_metrics:charts", args=[group]
                ),
                f"/dashboard/project/instance_metrics/charts/{group}/",
            )
        with self.assertRaises(NoReverseMatch):
            reverse("horizon:project:instance_metrics:charts", args=["gpu"])

    def test_panel_is_not_in_the_sidebar(self):
        class Panel:
            nav = True

        horizon = types.SimpleNamespace(Panel=Panel)
        name = "grian_ui.content.project_instance_metrics.panel"
        with mock.patch.dict(sys.modules, {"horizon": horizon}):
            sys.modules.pop(name, None)
            module = importlib.import_module(name)
            sys.modules.pop(name, None)
        panel = module.InstanceMetrics
        # horizon_nav() leaves nav = False panels out of the sidebar, and
        # Dashboard._decorated_urls() still registers their URLs
        self.assertIs(panel.nav, False)
        self.assertEqual(panel.slug, "instance_metrics")
        self.assertEqual(
            panel.urls, "grian_ui.content.project_instance_metrics.urls"
        )
        self.assertEqual(
            panel.permissions,
            (
                "openstack.services.compute",
                "openstack.services.metric-storage",
            ),
        )

    def test_enabled_file(self):
        from grian_ui.local.enabled import (
            _1105_project_instance_metrics_panel as enabled,
        )

        self.assertEqual(enabled.PANEL, "instance_metrics")
        self.assertEqual(enabled.PANEL_DASHBOARD, "project")
        self.assertEqual(enabled.PANEL_GROUP, "telemetry")
        self.assertEqual(
            enabled.ADD_PANEL,
            "grian_ui.content.project_instance_metrics.panel.InstanceMetrics",
        )
        self.assertFalse(hasattr(enabled, "ADD_INSTALLED_APPS"))
