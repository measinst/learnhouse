"""Tests for src/services/refresher/refresher.py — the learner refresher."""
from datetime import datetime, timedelta

from src.db.courses.certifications import CertificateUser, Certifications
from src.db.courses.courses import Course
from src.services.nudges.preferences import set_lifecycle_opt_out
from src.services.refresher.refresher import dedupe_key, render_body, run_refreshers

CFG = {"enabled": True, "days_after_certificate": 14, "subject": "Two quick questions",
       "intro": "Say them out loud.", "prompts": ["What is the one check before Send?", "Which number do you report?"],
       "link_text": "Open the course", "link_path": "/course/abc"}


async def _course_with_cert(db, org, user, *, days_ago, enabled=True, cid=901):
    course = Course(id=cid, name="Refresher course", description="d", public=True, published=True, open_to_contributors=False,
                    org_id=org.id, course_uuid=f"course_{cid}", creation_date=str(datetime.now()), update_date=str(datetime.now()),
                    extra_metadata={"lifecycle_refresher": dict(CFG, enabled=enabled)})
    db.add(course)
    await db.commit()
    await db.refresh(course)
    cert = Certifications(certification_uuid=f"certification_{cid}", course_id=course.id, config={}, creation_date=str(datetime.now()), update_date=str(datetime.now()))
    db.add(cert)
    await db.commit()
    await db.refresh(cert)
    cu = CertificateUser(user_id=user.id, certification_id=cert.id, user_certification_uuid=f"RD-{cid}-{user.id}",
                         created_at=str(datetime.now() - timedelta(days=days_ago)))
    db.add(cu)
    await db.commit()
    return course


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


class TestContent:
    def test_body_has_prompts_and_no_answers(self):
        body = render_body("Refresher course", CFG, "https://x/course/abc")
        assert "What is the one check before Send?" in body and "Open the course" in body
        assert 'href="https://x/course/abc"' in body

    def test_dedupe_key_is_per_course_and_user(self):
        assert dedupe_key("course_a", 7) != dedupe_key("course_a", 8) != dedupe_key("course_b", 8)
