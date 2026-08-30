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
a deploy. Nothing is sent for courses without that block.

Idempotency reuses the nudge ledger: one ``NudgeSend`` row per
``refresher:<course_uuid>:<user_id>``, claimed before the provider is called.
Learners who opted out of lifecycle email, or whose address is suppressed,
are skipped — the same ``EmailPreference`` rules as every other lifecycle mail.
Unlike the admin nudges this is not gated on SaaS mode: it is a feature of a
course, not of the platform's own growth funnel.
"""
import html
import logging
import os
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


def refresher_enabled() -> bool:
    return os.environ.get("LEARNHOUSE_REFRESHER_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def dedupe_key(course_uuid: str, user_id: int) -> str:
    return f"refresher:{course_uuid}:{user_id}"


def _parse_created(value: str) -> Optional[datetime]:
    """CertificateUser.created_at is a str(datetime.now()) string."""
    try:
        return datetime.fromisoformat(value[:26])
    except Exception:
        return None


def render_body(course_name: str, cfg: dict, course_url: str) -> str:
    prompts = "".join(
        f'<li style="margin:0 0 12px 0;font-size:16px;line-height:1.5;">{html.escape(p)}</li>'
        for p in cfg.get("prompts", [])[:3]
    )
    return (
        f'<p style="font-size:16px;line-height:1.6;">{html.escape(cfg.get("intro", ""))}</p>'
        f'<ol style="padding-left:22px;">{prompts}</ol>'
        f'<p style="margin-top:24px;"><a href="{html.escape(course_url)}" '
        f'style="display:inline-block;padding:12px 22px;background:#000;color:#fff;'
        f'border-radius:8px;text-decoration:none;font-weight:700;">'
        f'{html.escape(cfg.get("link_text") or "Open the course")}</a></p>'
        f'<p style="font-size:13px;color:#666;">{html.escape(course_name)}</p>'
    )


async def run_refreshers(db_session: AsyncSession, now: Optional[datetime] = None, dry_run: bool = False) -> dict:
    """Send due refreshers. Returns counts. Safe to call repeatedly."""
    from src.services.email.utils import send_email
    from src.services.users.emails import _email_layout

    now = now or datetime.now()
    stats = {"candidates": 0, "sent": 0, "skipped_opted_out": 0, "skipped_dup": 0, "failed": 0}
    if not refresher_enabled() and not dry_run:
        return stats

    courses = (await db_session.execute(select(Course))).scalars().all()
    for course in courses:
        cfg = (course.extra_metadata or {}).get("lifecycle_refresher") or {}
        if not cfg.get("enabled"):
            continue
        days = int(cfg.get("days_after_certificate", DEFAULT_DAYS))
        cert_stmt = select(CertificateUser).join(Certifications, Certifications.id == CertificateUser.certification_id).where(Certifications.course_id == course.id)
        cert_users = (await db_session.execute(cert_stmt)).scalars().all()
        if not cert_users:
            continue
        opted_out = await get_opted_out_user_ids(db_session, [cu.user_id for cu in cert_users])
        org = (await db_session.execute(select(Organization).where(Organization.id == course.org_id))).scalars().first()
        try:
            base = await links.org_base_url(org.slug, db_session, org.id) if org else ""
        except Exception:  # base URL resolution must never block a send
            base = ""
        media_base = get_media_base_url(None)
        for cu in cert_users:
            created = _parse_created(cu.created_at or "")
            if not created:
                continue
            age = (now - created).days
            if age < days or age > days + WINDOW_DAYS:
                continue
            stats["candidates"] += 1
            if cu.user_id in opted_out:
                stats["skipped_opted_out"] += 1
                continue
            key = dedupe_key(course.course_uuid, cu.user_id)
            exists = (await db_session.execute(select(NudgeSend).where(NudgeSend.dedupe_key == key))).scalars().first()
            if exists:
                stats["skipped_dup"] += 1
                continue
            user = (await db_session.execute(select(User).where(User.id == cu.user_id))).scalars().first()
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
            course_url = (cfg.get("link_url") or (base + cfg.get("link_path", ""))) if cfg.get("link_path") or cfg.get("link_url") else base
            unsubscribe = links.unsubscribe_url(media_base, user.user_uuid) if media_base else ""
            body = _email_layout(title=cfg.get("subject", "A quick refresher"), body_content=render_body(course.name, cfg, course_url),
                                 unsubscribe_url=unsubscribe)
            headers = {"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"} if unsubscribe else None
            ok = send_email(to=user.email, subject=cfg.get("subject", "A quick refresher"), body=body, headers=headers)
            row.status = NudgeSendStatus.SENT if ok is not False else NudgeSendStatus.FAILED
            if ok is False:
                row.error = "provider rejected or unavailable"
                stats["failed"] += 1
            else:
                row.sent_at = datetime.now(timezone.utc)
                stats["sent"] += 1
            db_session.add(row)
            await db_session.commit()
    return stats
