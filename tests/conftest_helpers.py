"""Shared test fixtures — two tenants, two deals, memberships."""
import contextlib
import os
import tempfile
import uuid

from fundos.core.models import Company, Deal, Membership, Tenant, User
from fundos.core.scoping import tenant_context


@contextlib.contextmanager
def temp_path(suffix=""):
    """Yield a filesystem path a library may create, write and reopen.

    NOT ``NamedTemporaryFile``. That yields an OPEN handle, and on Windows the
    handle holds an exclusive lock, so any library asked to write the same path
    — ``openpyxl.Workbook.save``, ``reportlab``, ``python-pptx`` — fails with
    PermissionError. The tests that use it pass on Linux and fail on Windows
    for a reason that has nothing to do with what they assert.

    A temp DIRECTORY has no such handle: the path inside it is free for anyone
    to create, and the whole directory is removed on exit whether or not the
    file was ever written.
    """
    with tempfile.TemporaryDirectory() as directory:
        yield os.path.join(directory, f"fixture{suffix}")


def make_world():
    """Two tenants; tenant A has two deals; tenant B one deal."""
    world = {}
    for label in ("a", "b"):
        tenant = Tenant.objects.create(name=f"T-{label}")
        with tenant_context(tenant.id):
            founder = User.objects.create_user(
                email=f"founder-{label}@x.io", tenant_id=tenant.id,
                name=f"Founder {label.upper()}")
            company = Company.objects.create(tenant_id=tenant.id,
                                             name=f"Co {label.upper()}")
            deal = Deal.objects.create(
                tenant_id=tenant.id, company=company,
                name=f"Deal {label.upper()}-1", round_type="seed",
                primary_owner=founder, created_by=founder)
            Membership.objects.create(
                tenant_id=tenant.id, user=founder, scope_type="deal",
                scope_id=deal.id, role="founder", status="active")
            world[label] = {"tenant": tenant, "founder": founder,
                            "company": company, "deal": deal}
    # second deal in tenant A owned by a second founder
    a = world["a"]
    with tenant_context(a["tenant"].id):
        founder2 = User.objects.create_user(
            email="founder-a2@x.io", tenant_id=a["tenant"].id, name="F A2")
        deal2 = Deal.objects.create(
            tenant_id=a["tenant"].id, company=a["company"],
            name="Deal A-2", round_type="seed", primary_owner=founder2,
            created_by=founder2)
        Membership.objects.create(
            tenant_id=a["tenant"].id, user=founder2, scope_type="deal",
            scope_id=deal2.id, role="founder", status="active")
        world["a2"] = {"founder": founder2, "deal": deal2}
    return world


def auth_headers(user):
    from fundos.core.auth import issue_tokens
    access, _ = issue_tokens(user)
    return {"HTTP_AUTHORIZATION": f"Bearer {access}"}
