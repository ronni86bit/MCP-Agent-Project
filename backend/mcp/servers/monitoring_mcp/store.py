"""In-memory fixture store for monitoring-mcp.

Data is regenerated deterministically from scenario.yaml at startup; windows
are filtered in process. No external monitoring backend exists in this phase.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from backend.mcp.fixtures.loader import ScenarioSpec, load_scenario
from backend.mcp.fixtures.monitoring_seed import MonitoringFixtures, build_monitoring_fixtures


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


class MonitoringStore:
    def __init__(self, spec: ScenarioSpec, fixtures: MonitoringFixtures):
        self.spec = spec
        self.services = list(fixtures.services)
        self._index: dict[str, list[tuple[datetime, dict]]] = {
            service: [(_parse(point["timestamp"]), point) for point in points]
            for service, points in fixtures.series.items()
        }
        self._alert_records = fixtures.alerts
        self._incident_records = fixtures.incidents

    @classmethod
    def load(cls) -> "MonitoringStore":
        spec = load_scenario()
        return cls(spec, build_monitoring_fixtures(spec))

    def is_service(self, service: str) -> bool:
        return service in self._index

    def valid_services(self) -> str:
        return ", ".join(sorted(self._index))

    def _points(self, service: str) -> list[tuple[datetime, dict]]:
        return self._index[service]

    def metrics(self, service: str, start: date, end: date, limit: int) -> list[dict]:
        window_start, _ = _day_bounds(start)
        _, window_end = _day_bounds(end)
        selected = [
            point
            for ts, point in self._points(service)
            if window_start <= ts < window_end
        ]
        return selected[:limit]

    def daily_error_rate(self, service: str, start: date, end: date) -> list[dict]:
        rows: list[dict] = []
        day = start
        while day <= end:
            day_start, day_end = _day_bounds(day)
            values = [
                (ts, point["error_rate"])
                for ts, point in self._points(service)
                if day_start <= ts < day_end
            ]
            if values:
                peak_ts, peak = max(values, key=lambda item: item[1])
                rows.append(
                    {
                        "date": day.isoformat(),
                        "avg_pct": round(sum(v for _, v in values) / len(values), 3),
                        "peak_pct": peak,
                        "peak_at": peak_ts.isoformat().replace("+00:00", "Z"),
                    }
                )
            day += timedelta(days=1)
        return rows

    def _overlaps_window(self, started: str, resolved: str | None, start: date, end: date) -> bool:
        window_start, _ = _day_bounds(start)
        _, window_end = _day_bounds(end)
        began = _parse(started)
        finished = _parse(resolved) if resolved else None
        return began < window_end and (finished is None or finished >= window_start)

    def incidents(self, start: date, end: date, service: str | None) -> list[dict]:
        return [
            record
            for record in self._incident_records
            if (service is None or record["service"] == service)
            and self._overlaps_window(record["started_at"], record["resolved_at"], start, end)
        ]

    def alerts(self, start: date, end: date, service: str | None, severity: str | None) -> list[dict]:
        window_start, _ = _day_bounds(start)
        _, window_end = _day_bounds(end)
        return [
            record
            for record in self._alert_records
            if (service is None or record["service"] == service)
            and (severity is None or record["severity"] == severity)
            and window_start <= _parse(record["fired_at"]) < window_end
        ]

    def compare(self, service: str, metric: str, a_start: date, a_end: date, b_start: date, b_end: date) -> dict:
        def stat(start: date, end: date) -> dict:
            window_start, _ = _day_bounds(start)
            _, window_end = _day_bounds(end)
            values = [
                (ts, point[metric])
                for ts, point in self._points(service)
                if window_start <= ts < window_end
            ]
            if not values:
                return {"avg": 0.0, "peak": 0.0, "peak_at": ""}
            peak_ts, peak = max(values, key=lambda item: item[1])
            avg = sum(v for _, v in values) / len(values)
            rounded = round(avg, 3) if isinstance(peak, float) else round(avg)
            return {"avg": rounded, "peak": peak, "peak_at": peak_ts.isoformat().replace("+00:00", "Z")}

        return {"window_a": stat(a_start, a_end), "window_b": stat(b_start, b_end)}
