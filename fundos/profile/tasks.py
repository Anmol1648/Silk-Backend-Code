"""Async profile generation (PRD §5.3)."""
import logging

from fundos.core.exceptions import GenerationAlreadyRunning

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="profile.generate")
def generate_profile_task(profile_id, job_id=None, user_id=None, force=False):
    """One orchestrated job covering every analysis source.

    Completes even when individual sources fail (C4) — a failed source
    marks its sections as needing input rather than failing the run, so the
    founder always ends up with a usable profile rather than an error.

    `force=True` bypasses the skip-if-unchanged gate. Without it, a repeat
    run over identical sources returns immediately instead of re-billing an
    identical result.
    """
    from fundos.core.models import User
    from fundos.core.services import jobs
    from fundos.profile.models import CompanyProfile
    from fundos.profile.services import generate_profile

    profile = CompanyProfile.objects.filter(id=profile_id).first()
    if not profile:
        logger.error("PROFILE: profile %s not found", profile_id)
        return {"status": "not_found"}

    user = User.objects.filter(id=user_id).first() if user_id else None

    try:
        return jobs.run_tracked(job_id, generate_profile, profile, user=user,
                                force=force)
    except GenerationAlreadyRunning as e:
        # NOT a failure. The lock did its job: a duplicate request (a
        # double-click, a retry, two tabs) declined to interleave with a run
        # already in progress. Logging it at ERROR with a full traceback, and
        # resetting status to draft, actively misrepresented a correct
        # outcome — and reset the status of the run that is still going.
        logger.info("PROFILE: duplicate generation request for %s ignored — "
                    "a run is already in progress. %s", profile_id, e)
        return {"status": "already_running", "detail": str(e)}
    except Exception:
        logger.exception("PROFILE: generation failed for %s", profile_id)
        profile.status = "draft"
        profile.save(update_fields=["status", "updated_at"])
        raise


@shared_task(name="profile.regenerate_section")
def regenerate_section_task(profile_id, section_key, user_id=None, force=False):
    from fundos.core.models import User
    from fundos.profile.models import CompanyProfile
    from fundos.profile.services import regenerate_section

    profile = CompanyProfile.objects.filter(id=profile_id).first()
    if not profile:
        return {"status": "not_found"}
    user = User.objects.filter(id=user_id).first() if user_id else None
    return regenerate_section(profile, section_key, user=user, force=force)
