"""Replay a captured LCU observation through real stores in a disposable workspace."""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from plugins.eventmail.store import EventMailStore
from plugins.lol_tracker.analysis import metrics, render
from plugins.lol_tracker.config import Config
from plugins.lol_tracker.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text(encoding="utf-8-sig"))
    if snapshot["status"] != "ok" or not snapshot["players"]:
        raise ValueError("Verification needs a successful real collector observation")
    with tempfile.TemporaryDirectory(prefix="akashic-lol-integration-") as folder:
        root = Path(folder)
        store = Store(root / "tracker.sqlite3")
        store.initialize()
        store.ingest(snapshot)
        captured = {p["platform"]+":"+p["riot_id"].casefold(): p for p in snapshot["players"]}
        before = {key: store.matches(key) for key in captured}
        store.ingest(snapshot)
        reopened = Store(root / "tracker.sqlite3")
        reopened.initialize()
        if {key: reopened.matches(key) for key in captured} != before:
            raise AssertionError("Repeated collection or restart changed existing match facts")
        wrong = copy.deepcopy(snapshot)
        wrong["observed_at"] = datetime.now(UTC).isoformat()
        wrong["players"][0]["matches"][0]["puuid"] = "different-player"
        try:
            reopened.ingest(wrong)
        except ValueError:
            pass
        else:
            raise AssertionError("A match for a different player was accepted")
        if reopened.latest() != snapshot or {key: reopened.matches(key) for key in captured} != before:
            raise AssertionError("Rejected observation partially changed durable state")

        end = datetime.fromisoformat(snapshot["observed_at"])
        facts = {"title": "isolated verification", "start": (end-timedelta(days=30)).isoformat(),
                 "end": end.isoformat(), "players": [{"riot_id": p["riot_id"], "server": p["platform"],
                 "observed_at": end.isoformat(), "coverage_note": "验证快照", "summary": metrics(before[key])}
                 for key, p in captured.items()]}
        report_id = "verification:daily"
        body = render(facts)
        store.claim(report_id, facts)
        store.finish(report_id, body)
        report = store.report(report_id)
        assert report is not None
        mail = EventMailStore(root / "eventmail.sqlite3")
        mail.initialize()
        payload = {"title": "隔离战绩报告", "text": report["body"]}
        kwargs = {"source_id": "lol_tracker", "event_id": report_id, "payload": payload,
                  "observed_at": datetime.fromisoformat(report["created_at"])}
        first = mail.report_alert(**kwargs)
        # Simulate loss of the producer's acknowledgement, then reopen both owners.
        mail = EventMailStore(root / "eventmail.sqlite3")
        second = mail.report_alert(**kwargs)
        if first["mail_id"] != second["mail_id"] or second["accepted"]:
            raise AssertionError("Lost acknowledgement caused a second Alert")
        store.published(report_id, dict(second))
        if Store(root / "tracker.sqlite3").pending():
            raise AssertionError("Report receipt did not survive reopening")
        config = Config()
        due, start, finish = config.daily_period(datetime(2026, 10, 9, 1, 0, tzinfo=UTC))
        if (str(start.date()), str(finish.date()), due.hour) != ("2026-10-08", "2026-10-09", 9):
            raise AssertionError("Daily report did not preserve the Shanghai calendar boundary")
        with sqlite3.connect(root / "eventmail.sqlite3") as db:
            mail_count = db.execute("SELECT COUNT(*) FROM mail_envelopes").fetchone()[0]
        result = {"successful": True, "players": len(captured), "matches": sum(map(len, before.values())),
                  "durable_alerts_after_replay": mail_count,
                  "checks": ["real observation import", "restart and collection deduplication",
                             "wrong-player rejection without partial writes", "report receipt persistence",
                             "EventMail lost-ack replay", "Shanghai daily boundary"]}
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
