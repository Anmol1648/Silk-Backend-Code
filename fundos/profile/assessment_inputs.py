"""Phase 3 — the profile-to-assessment bridge, step 1 half.

WHAT THIS MODULE EXISTS TO FIX
------------------------------
Until now `fundos/assessment/` contained no reference to the company profile.
Step 1 researched a company exhaustively and step 2 rated it from uploaded
documents alone, so the research never reached the rating. A founder who had
supplied no financial model scored blank on parameters their own website
answered.

This module closes that gap from the step 1 side: it emits TYPED VALUES under
the workbook's own `input_key` names, with unit, source and confidence per
field. The assessment reads them without translating anything, because the
contract is the workbook's contract.

WHY A TABLE RATHER THAN A PROFILE SECTION
-----------------------------------------
A section is prose plus a JSON blob, versioned as a document. These are 45
independent facts, each of which needs its own provenance, its own confidence,
and its own "where did this come from" answer when a founder disputes the
score it produced. Storing them as one blob makes per-field provenance
impossible to query and per-field regeneration impossible to do.

They are also DERIVED, not authored: regenerating the profile should refresh
them without going through section-version history, and a founder editing one
is editing a fact about their company, not a paragraph.

THE RULE, SAME AS DOCUMENT EXTRACTION
-------------------------------------
Facts have no opinions. This writes `value`, `unit`, `source_url`,
`source_type` and `confidence`. It NEVER writes a band or a score. The
assessment engine bands from published config, and that separation is what
lets a score be defended: the fact came from the company's careers page, the
threshold came from the published rubric, and neither was decided by a model.
"""
import uuid

from django.conf import settings
from django.db import models

# Source tiers (§4.3). Lower is better, and the bridge uses this to decide
# which of two answers for the same parameter wins: a figure in the company's
# own audited financial model outranks the same figure inferred from a press
# article, however confident the model was about the article.
TIER_PRIMARY_DOCUMENT = 1      # the founder's own uploaded documents
TIER_COMPANY_OWNED = 2         # the company's website, filings, LinkedIn
TIER_REPUTABLE_THIRD_PARTY = 3  # press, databases, registries
TIER_INFERRED = 4              # derived or estimated from the above

SOURCE_TYPES = [
    ("profile", "Company profile research"),
    ("document", "Uploaded document"),
    ("benchmark", "Sector benchmark table"),
    ("founder", "Founder-entered"),
    ("computed", "Derived from other profile data"),
]


class ProfileAssessmentInput(models.Model):
    """One typed, sourced value for one assessment parameter.

    Keyed by (profile, input_key) so a refresh updates in place rather than
    accumulating duplicates — this is a current-state table, and its history
    lives in the assessments that consumed it, each of which is immutable.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    profile = models.ForeignKey("companyprofile.CompanyProfile",
                                on_delete=models.CASCADE,
                                related_name="assessment_inputs")

    input_key = models.CharField(
        max_length=48, db_index=True,
        help_text="The workbook's own parameter key, e.g. TEAM_COFDR_YRS. "
                  "Not a FundOS name — matching the workbook exactly is what "
                  "removes the translation layer between step 1 and step 2.")
    value = models.CharField(
        max_length=255, blank=True, default="",
        help_text="The raw value as extracted. Kept as text because the "
                  "engine parses it, and because 'about 40' is more honest "
                  "than silently rounding to 40.")
    unit = models.CharField(max_length=32, blank=True, default="")

    source_type = models.CharField(max_length=32, choices=SOURCE_TYPES,
                                   default="profile")
    source_url = models.CharField(
        max_length=1024, blank=True, default="",
        help_text="Where this was found. Required in spirit: Evidence & "
                  "Workings has to name a source per figure.")
    source_detail = models.TextField(blank=True, default="")
    source_tier = models.IntegerField(
        null=True, blank=True,
        help_text="1 primary document, 2 company-owned, 3 reputable third "
                  "party, 4 inferred. Decides which value wins when two "
                  "sources disagree.")
    confidence = models.DecimalField(max_digits=4, decimal_places=2,
                                     null=True, blank=True)
    justification = models.TextField(blank=True, default="")

    # A founder correcting a researched value is the highest-quality signal
    # available, so it is marked and never overwritten by a later run.
    is_founder_confirmed = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    confirmed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "company_profile_assessment_input"
        unique_together = [("profile", "input_key")]
        indexes = [models.Index(fields=["profile", "input_key"])]
        ordering = ("input_key",)

    def __str__(self):
        return f"{self.input_key}={self.value}"
