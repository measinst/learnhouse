"""Learner refresher: one email, N days after a learner earns a course's
certificate, that asks two recall questions and links back to the course.

Retrieval practice after a delay beats re-reading (Roediger & Karpicke); the
email deliberately contains no answers.

Per-course configuration lives on the course itself, in
``Course.extra_metadata["lifecycle_refresher"]``::

    {"enabled": true, "days_after_certificate": 14,
     "subject": "...", "intro": "...", "prompts": ["...", "..."],
     "link_text": "Open the course", "link_path": "/course/<uuid>"}

so course authors (or a course repo's sync tool) control the content without
a deploy. Nothing is sent for courses without that block. ``link_url`` (an
absolute http(s) URL) may be given instead of ``link_path``; anything else is
dropped rather than mailed, because a course author is not trusted to put an
arbitrary scheme in a button.

Idempotency reuses the nudge ledger: one ``NudgeSend`` row per
``refresher:<course_uuid>:<user_id>``, claimed before the provider is called.
A provider failure is recorded on that row as FAILED and is not retried: the
window is short, and a second attempt tomorrow would land as a duplicate for
the learners the provider did accept.
Learners who opted out of lifecycle email, or whose address is suppressed,
are skipped — the same ``EmailPreference`` rules as every other lifecycle mail.
Unlike the admin nudges this is not gated on SaaS mode: it is a feature of a
course, not of the platform's own growth funnel.

One course's bad configuration or query never stops the others: each course is
processed inside its own guard and a failure is logged and counted.
"""
import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.courses.certifications import CertificateUser, Certifications
from src.db.courses.courses import Course
from src.db.nudges import NudgeSend, NudgeSendStatus
from src.db.organizations import Organization
from src.db.users import User
from src.services.nudges import links
from src.services.email.utils import get_media_base_url
from src.services.nudges.preferences import get_opted_out_user_ids

logger = logging.getLogger(__name__)

NUDGE_ID = "learner.course_refresher"
DEFAULT_DAYS = 14
# A certificate older than days + WINDOW_DAYS is left alone: a refresher that
# arrives a month late is noise, not reinforcement.
WINDOW_DAYS = 3
# Per-run ceiling on provider calls. The daily volume is small; this exists so
# a backfill (a course enabling the refresher with hundreds of recent
# certificates, or a run that missed several days) cannot turn into a burst
# that trips provider rate limits. Anything left over is still inside the
# window tomorrow.
MAX_SENDS_PER_RUN = 500


def refresher_enabled() -> bool:
    return os.environ.get("LEARNHOUSE_REFRESHER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def dedupe_key(course_uuid: str, user_id: int) -> str:
    return f"refresher:{course_uuid}:{user_id}"


def _parse_created(value: str) -> Optional[datetime]:
    """CertificateUser.created_at is a ``str(datetime.now())`` string — naive,
    in the API process's local time. Anything ISO-ish is accepted; an aware
    value is converted to the same naive local time so it compares with
    ``datetime.now()``. Unparseable values yield None and the row is skipped."""
    try:
        parsed = datetime.fromisoformat(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _course_url(cfg: dict, base: str) -> str:
    """Absolute http(s) link for the button, or "" when none can be built."""
    url = cfg.get("link_url")
    if not url and cfg.get("link_path") and base:
        url = base + str(cfg["link_path"])
    url = str(url or "").strip()
    if url.lower().startswith(("http://", "https://")):
        return url
    return base if base else ""


def _prompts(cfg: dict) -> list:
    prompts = cfg.get("prompts")
    if not isinstance(prompts, (list, tuple)):
        return []
    return [str(p) for p in prompts[:3]]


def render_body(course_name: str, cfg: dict, course_url: str) -> str:
    prompts = "".join(
        f'<li style="margin:0 0 12px 0;font-size:16px;line-height:1.5;">{html.escape(p)}</li>'
        for p in _prompts(cfg)
    )
    button = ""
    if course_url:
        button = (
            f'<p style="margin-top:24px;"><a href="{html.escape(course_url)}" '
            f'style="display:inline-block;padding:12px 22px;background:#000;color:#fff;'
            f'border-radius:8px;text-decoration:none;font-weight:700;">'
            f'{html.escape(str(cfg.get("link_text") or "Open the course"))}</a></p>'
        )
    return (
        f'<p style="font-size:16px;line-height:1.6;">{html.escape(str(cfg.get("intro") or ""))}</p>'
        f'<ol style="padding-left:22px;">{prompts}</ol>'
        f"{button}"
        f'<p style="font-size:13px;color:#666;">{html.escape(course_name)}</p>'
    )


def _days_after(cfg: dict) -> int:
    try:
        return max(0, int(cfg.get("days_after_certificate", DEFAULT_DAYS)))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


async def run_refreshers(db_session: AsyncSession, now: Optional[datetime] = None, dry_run: bool = False) -> dict:
    """Send due refreshers. Returns counts. Safe to call repeatedly."""
    now = now or datetime.now()
    stats = {"candidates": 0, "sent": 0, "skipped_opted_out": 0, "skipped_dup": 0, "failed": 0, "errors": 0}
    if not refresher_enabled() and not dry_run:
        return stats

    # Plain values, not ORM rows: a rollback after one course's failure
    # expires every loaded instance, and touching an expired attribute is
    # implicit IO — which an async session refuses (MissingGreenlet).
    courses = [
        _CourseRef(id=c.id, course_uuid=c.course_uuid, name=c.name, org_id=c.org_id,
                   cfg=(c.extra_metadata or {}).get("lifecycle_refresher") or {})
        for c in (await db_session.execute(select(Course))).scalars().all()
    ]
    for course in courses:
        if not isinstance(course.cfg, dict) or not course.cfg.get("enabled"):
            continue
        if stats["sent"] >= MAX_SENDS_PER_RUN:
            logger.info("Refresher run reached %s sends; the rest wait for tomorrow", MAX_SENDS_PER_RUN)
            break
        try:
            await _run_course(db_session, course, course.cfg, now, dry_run, stats)
        except Exception:
            await db_session.rollback()
            stats["errors"] += 1
            logger.exception("Refresher run failed for course %s; continuing", course.course_uuid)
    return stats


@dataclass(frozen=True)
class _CourseRef:
    id: int
    course_uuid: str
    name: str
    org_id: int
    cfg: dict


async def _run_course(db_session: AsyncSession, course: _CourseRef, cfg: dict, now: datetime, dry_run: bool, stats: dict) -> None:
    from src.services.users.emails import _email_layout, _send_notification_email

    days = _days_after(cfg)
    cert_stmt = (
        select(CertificateUser)
        .join(Certifications, Certifications.id == CertificateUser.certification_id)
        .where(Certifications.course_id == course.id)
    )
    due = []
    for cu in (await db_session.execute(cert_stmt)).scalars().all():
        created = _parse_created(cu.created_at or "")
        if not created:
            continue
        age = (now - created).days
        if days <= age <= days + WINDOW_DAYS:
            due.append(cu)
    if not due:
        return
    stats["candidates"] += len(due)

    user_ids = [cu.user_id for cu in due]
    opted_out = await get_opted_out_user_ids(db_session, user_ids)
    # One query each for the ledger and the users, rather than one per learner.
    keys = {cu.user_id: dedupe_key(course.course_uuid, cu.user_id) for cu in due}
    already = set(
        (await db_session.execute(select(NudgeSend.dedupe_key).where(NudgeSend.dedupe_key.in_(list(keys.values()))))).scalars().all()
    )
    users = {
        u.id: u for u in (await db_session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
    }

    org = (await db_session.execute(select(Organization).where(Organization.id == course.org_id))).scalars().first()
    try:
        base = await links.org_base_url(org.slug, db_session, org.id) if org else ""
    except Exception:  # base URL resolution must never block a send
        logger.warning("Refresher: could not resolve a base URL for org %s; sending without a link", course.org_id)
        base = ""
    media_base = get_media_base_url(None)
    subject = str(cfg.get("subject") or "A quick refresher")
    course_url = _course_url(cfg, base)

    for cu in due:
        if stats["sent"] >= MAX_SENDS_PER_RUN:
            return
        if cu.user_id in opted_out:
            stats["skipped_opted_out"] += 1
            continue
        key = keys[cu.user_id]
        if key in already:
            stats["skipped_dup"] += 1
            continue
        user = users.get(cu.user_id)
        if not user or not user.email or "@" not in user.email:
            continue
        if dry_run:
            stats["sent"] += 1
            logger.info("[dry-run] refresher -> %s (%s)", user.email, course.name)
            continue
        row = NudgeSend(nudge_id=NUDGE_ID, dedupe_key=key, org_id=course.org_id, user_id=cu.user_id,
                        status=NudgeSendStatus.CLAIMED, lang="en", claimed_at=datetime.now(timezone.utc))
        db_session.add(row)
        try:
            await db_session.commit()  # the unique dedupe_key is the race boundary
        except Exception:
            await db_session.rollback()
            stats["skipped_dup"] += 1
            continue
        unsubscribe = links.unsubscribe_url(media_base, user.user_uuid) if media_base else ""
        body = _email_layout(title=subject, body_content=render_body(course.name, cfg, course_url),
                             unsubscribe_url=unsubscribe)
        headers = {"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if unsubscribe else None
        # Swallows provider errors and returns False: a refresher must never
        # abort the run for every other learner, and the ledger row must not
        # be left CLAIMED (which would read as a crash mid-send).
        ok = _send_notification_email(to=user.email, subject=subject, body=body, headers=headers)
        if ok is False:
            row.status = NudgeSendStatus.FAILED
            row.error = "provider rejected or unavailable"
            stats["failed"] += 1
        else:
            row.status = NudgeSendStatus.SENT
            row.sent_at = datetime.now(timezone.utc)
            if isinstance(ok, dict) and ok.get("id"):
                row.provider_id = str(ok["id"])
            stats["sent"] += 1
        db_session.add(row)
        await db_session.commit()
