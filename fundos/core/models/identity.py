"""
M0.1 Tenancy & identity (Doc 2 M0.1, Doc 1 M0-§1).

Email is the identity (BR-M0-001). Invite-only onboarding (GC-06).
Case-insensitive email uniqueness via a functional partial index
(Doc 6 §11) — added in the initial migration.
"""
import hashlib
import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone

from fundos.core.models.base import BaseModel
from fundos.core.scoping import TenantManager


class Tenant(models.Model):
    """Customer-organisation data boundary, resolved at login (never URL)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    idp_org_id = models.CharField(max_length=255, blank=True, default="", db_index=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "tenant"

    def __str__(self):
        return self.name


class UserManager(BaseUserManager):
    def create_user(self, email, password=None, **extra):
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra)
        user.set_password(password or uuid.uuid4().hex)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_internal", True)
        return self.create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    """`user` (Doc 2 M0.1). Email is the identity; UUID PK; soft delete.

    phone_msisdn: normalised E.164 digits — WhatsApp recipient resolution
    from deal membership (Doc 2 M0.7 addition).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    email = models.EmailField(max_length=255)
    name = models.CharField(max_length=255, blank=True, default="")
    idp_subject = models.CharField(max_length=255, blank=True, default="", db_index=True)
    is_internal = models.BooleanField(default=False)  # FundOS staff
    phone_msisdn = models.CharField(max_length=15, blank=True, default="")
    phone_verified = models.BooleanField(default=False)
    notification_channels = models.JSONField(
        default=dict, blank=True,
        help_text='e.g. {"in_app": true, "email": true, "whatsapp": false}',
    )
    # Gap G3: POST /contexts/switch records the user's last-active deal for
    # landing-page routing and notification context (pure UX — never authz).
    last_active_deal_id = models.UUIDField(null=True, blank=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = "email"
    objects = UserManager()

    class Meta:
        db_table = "user"
        constraints = [
            # UQp on lower(email) WHERE not deleted (GC-08 / Appendix 2-C)
            models.UniqueConstraint(
                models.functions.Lower("email"),
                condition=models.Q(is_deleted=False),
                name="uq_user_email_live",
            ),
        ]

    def __str__(self):
        return self.email


class Company(BaseModel):
    """`company` (Doc 2 M0.1)."""

    name = models.CharField(max_length=255)
    domain = models.CharField(max_length=255, blank=True, default="")
    hq_country = models.CharField(max_length=64, blank=True, default="")

    class Meta:
        db_table = "company"
        constraints = [
            models.UniqueConstraint(
                models.functions.Lower("domain"),
                condition=models.Q(is_deleted=False) & ~models.Q(domain=""),
                name="uq_company_domain_live",
            ),
        ]

    def __str__(self):
        return self.name


class Deal(BaseModel):
    """`deal` a.k.a. fundraising round (Doc 2 M0.1).

    Naming note: the funding-events feed entity is `market_deal`,
    deliberately distinct.
    """

    STATUS = (("active", "active"), ("closed", "closed"),
              ("declined", "declined"), ("archived", "archived"))

    company = models.ForeignKey(Company, on_delete=models.PROTECT, related_name="deals")
    name = models.CharField(max_length=255)
    round_type = models.CharField(max_length=32, blank=True, default="")  # FK→m_round_type
    status = models.CharField(max_length=16, choices=STATUS, default="active")
    primary_owner = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="owned_deals")

    class Meta:
        db_table = "deal"

    def __str__(self):
        return f"{self.company.name} / {self.name}"


class Membership(BaseModel):
    """`membership` — user × scope × role (Doc 2 M0.1 / BR-M0-002).

    company-scoped membership grants the role across all present & future
    deals of that company; deal-scoped grants that one deal. Effective role
    on a deal = max(company_role, deal_role) (GC-07).
    """

    SCOPE = (("company", "company"), ("deal", "deal"))
    ROLES = (
        ("founder", "founder"), ("co_founder", "co_founder"), ("team", "team"),
        ("advisor", "advisor"), ("banker", "banker"), ("lead_banker", "lead_banker"),
        ("fundos_admin", "fundos_admin"), ("fundos_analyst", "fundos_analyst"),
        ("data_steward", "data_steward"),
    )
    STATUS = (("pending", "pending"), ("active", "active"))

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    scope_type = models.CharField(max_length=8, choices=SCOPE)
    scope_id = models.UUIDField()
    role = models.CharField(max_length=16, choices=ROLES)
    is_secondary_owner = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=STATUS, default="pending")
    invited_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        db_table = "membership"
        indexes = [models.Index(fields=["scope_type", "scope_id"]),
                   models.Index(fields=["user"])]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "scope_type", "scope_id", "role"],
                condition=models.Q(is_deleted=False),
                name="uq_membership_live",
            ),
        ]


class OtpToken(models.Model):
    """OTP mechanics — exact semantics per Doc 6 Appendix E.

    Hashed OTP; TTL; attempts_left (default 5); a wrong guess decrements
    attempts without extending expiry; exhausting attempts deletes the row;
    success consumes the row (single-use); store-create replaces any prior
    open OTP for the identity.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    identity = models.CharField(max_length=255, db_index=True)  # lower(email)
    otp_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts_left = models.SmallIntegerField(default=5)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "otp_token"

    @staticmethod
    def hash_otp(otp: str) -> str:
        return hashlib.sha256(otp.encode()).hexdigest()

    @classmethod
    def issue(cls, identity: str, otp: str, ttl_seconds: int, max_attempts: int):
        identity = identity.strip().lower()
        cls.objects.filter(identity=identity).delete()  # replace prior open OTP
        return cls.objects.create(
            identity=identity,
            otp_hash=cls.hash_otp(otp),
            expires_at=timezone.now() + timezone.timedelta(seconds=ttl_seconds),
            attempts_left=max_attempts,
        )

    @classmethod
    def verify(cls, identity: str, otp: str) -> str:
        """Returns 'ok' | 'expired' | 'incorrect' | 'too_many_attempts' | 'error'."""
        identity = (identity or "").strip().lower()
        try:
            row = cls.objects.filter(identity=identity).order_by("-created_at").first()
            if not row:
                return "incorrect"
            if timezone.now() > row.expires_at:
                row.delete()
                return "expired"
            if row.otp_hash == cls.hash_otp(otp or ""):
                row.delete()  # success consumes the row
                return "ok"
            row.attempts_left -= 1
            if row.attempts_left <= 0:
                row.delete()
                return "too_many_attempts"
            row.save(update_fields=["attempts_left"])  # no expiry extension
            return "incorrect"
        except Exception:
            return "error"
