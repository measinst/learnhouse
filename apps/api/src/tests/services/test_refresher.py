"""Tests for src/services/refresher/ — the learner refresher and its tick."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlmodel import select

from src.db.courses.certifications import CertificateUser, Certifications
from src.db.courses.courses import Course
from src.db.nudges import NudgeSend, NudgeSendStatus
from src.services.nudges.preferences import set_lifecycle_opt_out
from src.services.refresher import scheduler
from src.services.refresher.refresher import dedupe_key, render_body, run_refreshers

CFG = {"enabled": True, "days_after_certificate": 14, "subject": "Two quick questions",
       "intro": "Say them out loud.", "prompts": ["What is the one check before Send?", "Which number do you report?"],
       "link_text": "Open the course", "link_path": "/course/abc"}

_SEND = "src.services.users.emails._send_notification_email"


async def _course_with_cert(db, org, user, *, days_ago, enabled=True, cid=901, cfg=None, created_at=None):
    course = Course(id=cid, name="Refresher course", description="d", public=True, published=True, open_to_contributors=False,
                    org_id=org.id, course_uuid=f"course_{cid}", creation_date=str(datetime.now()), update_date=str(datetime.now()),
                    extra_metadata={"lifecycle_refresher": dict(cfg or CFG, enabled=enabled)})
    db.add(course)
    await db.commit()
    await db.refresh(course)
    cert = Certifications(certification_uuid=f"certification_{cid}", course_id=course.id, config={}, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(cert)
    await db.commit()
    await db.refresh(cert)
    cu = CertificateUser(user_id=user.id, certification_id=cert.id, user_certification_uuid=f"RD-{cid}-{user.id}",
                         created_at=created_at or str(datetime.now() - timedelta(days=days_ago)))
    db.add(cu)
    await db.commit()
    return course


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("LEARNHOUSE_REFRESHER_ENABLED", "true")


@pytest.fixture
def sender():
    """Patch the provider call and record what would go out."""
    with patch(_SEND) as mock:
        mock.return_value = {"id": "msg_1"}
        yield mock


class TestEligibility:
    async def test_due_certificate_is_a_candidate(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=14)
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 1 and stats["sent"] == 1

    async def test_too_early_and_too_late_are_ignored(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=5, cid=902)
        await _course_with_cert(db, org, regular_user, days_ago=40, cid=903)
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 0

    async def test_disabled_course_sends_nothing(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=14, enabled=False, cid=904)
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 0

    async def test_opted_out_learner_is_skipped(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=15, cid=905)
        await set_lifecycle_opt_out(db, regular_user.id)
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 1 and stats["skipped_opted_out"] == 1 and stats["sent"] == 0

    async def test_switched_off_sends_nothing_outside_dry_run(self, db, org, regular_user, monkeypatch):
        monkeypatch.delenv("LEARNHOUSE_REFRESHER_ENABLED", raising=False)
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=906)
        stats = await run_refreshers(db)
        assert stats["candidates"] == 0 and stats["sent"] == 0

    async def test_aware_created_at_is_accepted(self, db, org, regular_user):
        """created_at is normally str(datetime.now()) — naive local — but an
        ISO string with an offset must not blow up the comparison."""
        aware = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        await _course_with_cert(db, org, regular_user, days_ago=0, cid=907, created_at=aware)
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 1

    async def test_unparseable_created_at_is_skipped(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=0, cid=908, created_at="not a date")
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 0

    async def test_bad_days_value_falls_back_to_default(self, db, org, regular_user):
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=909, cfg=dict(CFG, days_after_certificate="soon"))
        stats = await run_refreshers(db, dry_run=True)
        assert stats["candidates"] == 1


class TestSending:
    async def test_second_run_sends_nothing(self, db, org, regular_user, enabled, sender):
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=910)
        first = await run_refreshers(db)
        second = await run_refreshers(db)
        assert first["sent"] == 1 and sender.call_count == 1
        assert second["sent"] == 0 and second["skipped_dup"] == 1 and sender.call_count == 1

        row = (await db.execute(select(NudgeSend).where(NudgeSend.dedupe_key == dedupe_key("course_910", regular_user.id)))).scalars().first()
        assert row.status == NudgeSendStatus.SENT and row.provider_id == "msg_1" and row.sent_at is not None

    async def test_mail_carries_unsubscribe_and_the_course_link(self, db, org, regular_user, enabled, sender, monkeypatch):
        monkeypatch.setenv("LEARNHOUSE_MEDIA_URL", "https://api.example.test")
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=911, cfg=dict(CFG, link_url="https://x.test/course/abc"))
        await run_refreshers(db)
        kwargs = sender.call_args.kwargs
        assert kwargs["to"] == regular_user.email and kwargs["subject"] == "Two quick questions"
        assert 'href="https://x.test/course/abc"' in kwargs["body"]
        assert "List-Unsubscribe" in kwargs["headers"]

    async def test_provider_failure_is_recorded_not_raised(self, db, org, regular_user, enabled, sender):
        """The ledger row must not stay CLAIMED, and the run must go on."""
        sender.return_value = False
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=912)
        stats = await run_refreshers(db)
        assert stats["failed"] == 1 and stats["sent"] == 0
        row = (await db.execute(select(NudgeSend).where(NudgeSend.dedupe_key == dedupe_key("course_912", regular_user.id)))).scalars().first()
        assert row.status == NudgeSendStatus.FAILED and row.error

    async def test_a_broken_course_does_not_stop_the_others(self, db, org, regular_user, enabled, sender):
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=913)
        await _course_with_cert(db, org, regular_user, days_ago=14, cid=914)
        async def explode(db_session, course, cfg, now, dry_run, stats):
            if course.id == 913:
                raise RuntimeError("boom")
            return await original(db_session, course, cfg, now, dry_run, stats)

        from src.services.refresher import refresher as mod
        original = mod._run_course
        with patch.object(mod, "_run_course", explode):
            stats = await run_refreshers(db)
        assert stats["errors"] == 1 and stats["sent"] == 1


class TestContent:
    def test_body_has_prompts_and_no_answers(self):
        body = render_body("Refresher course", CFG, "https://x/course/abc")
        assert "What is the one check before Send?" in body and "Open the course" in body
        assert 'href="https://x/course/abc"' in body

    def test_body_escapes_author_text(self):
        cfg = dict(CFG, intro="<script>alert(1)</script>", prompts=["a & b"], link_text='"><img>')
        body = render_body("R & D", cfg, "https://x/?a=1&b=2")
        assert "<script>" not in body and "&lt;script&gt;" in body
        assert "a &amp; b" in body and "R &amp; D" in body
        assert 'href="https://x/?a=1&amp;b=2"' in body and "<img>" not in body

    def test_non_http_link_url_is_dropped(self):
        from src.services.refresher.refresher import _course_url
        assert _course_url({"link_url": "javascript:alert(1)"}, "https://base") == "https://base"
        assert _course_url({"link_url": "javascript:alert(1)"}, "") == ""
        assert _course_url({"link_path": "/course/abc"}, "") == ""
        assert _course_url({"link_path": "/course/abc"}, "https://base") == "https://base/course/abc"

    def test_no_link_means_no_button(self):
        assert "<a " not in render_body("c", CFG, "")

    def test_prompts_must_be_a_list(self):
        assert "<li" not in render_body("c", dict(CFG, prompts="one string"), "")

    def test_dedupe_key_is_per_course_and_user(self):
        assert dedupe_key("course_a", 7) != dedupe_key("course_a", 8) != dedupe_key("course_b", 8)


class TestScheduler:
    @pytest.fixture(autouse=True)
    def _clean(self):
        scheduler._task = None
        yield
        scheduler._task = None

    def test_does_not_start_when_the_feature_is_off(self, monkeypatch, caplog):
        monkeypatch.delenv("LEARNHOUSE_REFRESHER_ENABLED", raising=False)
        with caplog.at_level("INFO"):
            scheduler.start_scheduler()
        assert scheduler._task is None
        assert "LEARNHOUSE_REFRESHER_ENABLED" in caplog.text

    async def test_starts_and_stops_when_enabled(self, enabled):
        scheduler.start_scheduler()
        assert scheduler._task is not None
        await scheduler.stop_scheduler()
        assert scheduler._task is None

    async def test_stopping_when_never_started_is_harmless(self):
        await scheduler.stop_scheduler()

    def test_exactly_on_the_hour_waits_a_full_day(self):
        on_hour = datetime(2026, 8, 30, scheduler.RUN_AT_HOUR_UTC, tzinfo=timezone.utc)
        assert scheduler._seconds_until_next_run(on_hour) == 24 * 3600
