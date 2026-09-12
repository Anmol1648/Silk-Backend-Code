"""Stage 2 Phase 1 — the Deal Scorecard API.

WHY THIS FILE DID NOT EXIST
---------------------------
The scoring engine, its seven weighted categories, 52 stage-dependent rubrics,
24 qualitative anchors, weight redistribution and override cascade were all
built and correct. Nothing referenced them. `fundos.assessment` was imported by
its own seeder, settings, and a test file — no views, no routes, no serialisers,
and no code path anywhere that created a ParameterValue outside tests.

This module is that connection. It adds no scoring logic: `run_scoring()`
already does the work and everything here reads what it produced.

TWO THINGS THE ENGINE COMPUTED THAT NOTHING ACTED ON
----------------------------------------------------
  * `input_coverage_pct` — now gates the headline score (D-09). Below the
    floor the overall number is SUPPRESSED rather than shown with a caveat.
    A number with a warning beside it still gets quoted in a meeting; an
    absent number with a list of missing documents gets the documents sent.

  * `applied_weight` vs `weight` — now two columns on the row, so weight
    redistributed away from an unassessable parameter is visible rather than
    being an invisible adjustment to the arithmetic.
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from fundos.assessment.models import (Assessment, CategoryScore, ConfigAnchor,
                                      ConfigParameter, ConfigRubric,
                                      ConfigWeight, ParameterValue)
from fundos.core.api.base import DealScopedAPIView, queue_generation

logger = logging.getLogger(__name__)

COVERAGE_FLOOR_PCT = 60
BAND_LABELS = {"excellent": "Excellent", "good": "Good", "fair": "Fair",
               "poor": "Poor", "": "Not assessed"}
NEXT_BAND = {"poor": ("fair", 5.0), "fair": ("good", 7.0),
             "good": ("excellent", 9.0)}


def _active(deal):
    """The current assessment for a deal.

    'scored' and 'final' both count; 'superseded' never does. The engine never
    overwrites a prior assessment, so the newest non-superseded row is the one
    on screen.
    """
    return (Assessment.objects.filter(deal=deal)
            .exclude(status="superseded")
            .order_by("-created_at").first())


def _label(band):
    return BAND_LABELS.get(band or "", "Not assessed")


def _band_from_score(score):
    """Display band for a rolled-up score.

    Roll-ups are weighted averages and land between anchors, so they are
    banded by proximity FOR DISPLAY ONLY. This never feeds back into scoring —
    the engine bands leaves from config and rolls the numbers up. This is a
    label on the result, not an input to it.
    """
    if score is None:
        return ""
    s = float(score)
    if s >= 8.0:
        return "excellent"
    if s >= 6.0:
        return "good"
    if s >= 4.0:
        return "fair"
    return "poor"


# ---------------------------------------------------------------------------
# Config resolution — platform rows, overridden by tenant rows
# ---------------------------------------------------------------------------

def _param_config(tenant_id):
    base = {p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id__isnull=True, is_active=True)}
    base.update({p.input_key: p for p in ConfigParameter.objects.filter(
        tenant_id=tenant_id, is_active=True)})
    return base


def _weights():
    """Weight rows keyed (level, code). NULL tenant first; tenant overwrites."""
    out = {}
    for w in ConfigWeight.objects.filter(is_active=True).order_by("tenant_id"):
        out[(w.level, w.code)] = w
    return out


def _cfg_attr(obj, *names, default=""):
    """Read the first attribute that exists.

    The rubric and anchor tables are seeded from a spec file and their column
    names have shifted between config generations. Probing a few known spellings
    keeps the drawer working across versions instead of 500-ing on a rename.
    """
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v not in (None, ""):
                return v
    return default


def _serialise_value(pv, cfg, weight):
    assessable = pv.score is not None and not pv.is_reference_only
    return {
        "key": pv.input_key,
        "refCode": pv.ref_code,
        "label": _cfg_attr(cfg, "label", "name", default="") if cfg else "",
        "category": pv.category,
        "weight": float(weight) if weight is not None else None,
        # A parameter with no score contributed nothing; the engine
        # redistributed its weight across its siblings.
        "appliedWeight": (float(weight) if (weight is not None and assessable)
                          else 0.0),
        "systemScore": (float(pv.system_score)
                        if pv.system_score is not None else None),
        "score": float(pv.score) if pv.score is not None else None,
        "band": pv.band or "",
        "bandLabel": _label(pv.band),
        "systemBand": pv.system_band or "",
        "isOverridden": pv.is_overridden,
        "overrideComment": pv.override_comment or "",
        "assessable": assessable,
        "rawValue": pv.raw_value,
        "unit": pv.unit,
        "confidence": (float(pv.confidence)
                       if pv.confidence is not None else None),
        "sourceType": pv.source_type or "",
        "isReferenceOnly": pv.is_reference_only,
    }


def _build_scorecard(assessment):
    """Categories → sub-items → parameters, mirroring the engine's own tree."""
    tenant_id = getattr(assessment, "tenant_id", None)
    values = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    params = _param_config(tenant_id)
    weights = _weights()
    cat_scores = {c.category_code: c
                  for c in CategoryScore.objects.filter(assessment=assessment)}

    leaves = {}
    for key, p in params.items():
        if not p.feeds_score:
            continue
        pv = values.get(key)
        if pv is None:
            continue
        parent = p.parent_code or p.category_code
        w = weights.get(("child", p.ref_code))
        row = _serialise_value(pv, p, w.weight if w else None)
        if not row["label"]:
            row["label"] = pv.ref_code or key
        leaves.setdefault(parent, []).append(row)

    categories = []
    cat_weights = sorted([w for (lvl, _c), w in weights.items()
                          if lvl == "category"], key=lambda w: w.code)
    for cw in cat_weights:
        cs = cat_scores.get(cw.code)
        subs = sorted([w for (lvl, _c), w in weights.items()
                       if lvl == "subitem" and w.parent_code == cw.code],
                      key=lambda w: w.code)
        sub_items = []
        for si in subs:
            kids = sorted(leaves.get(si.code, []), key=lambda k: k["key"])
            scored = [k["score"] for k in kids if k["score"] is not None]
            sub_items.append({
                "code": si.code,
                "label": si.label or si.code,
                "weight": float(si.weight),
                "score": round(sum(scored) / len(scored), 2) if scored else None,
                "parameters": kids,
            })

        direct = sorted(leaves.get(cw.code, []), key=lambda k: k["key"])
        if direct:
            sub_items.append({"code": cw.code, "label": "Direct parameters",
                              "weight": 100.0, "score": None,
                              "parameters": direct})

        score = cs.score if cs else None
        categories.append({
            "code": cw.code,
            "label": cw.label or cw.code,
            "weight": float(cw.weight),
            "appliedWeight": float(cs.applied_weight) if cs else 0.0,
            "score": float(score) if score is not None else None,
            "band": _band_from_score(score),
            "bandLabel": _label(_band_from_score(score)),
            "subItems": sub_items,
        })
    return categories


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

class AssessmentView(DealScopedAPIView):
    """The full scorecard."""

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        a = _active(self.deal)
        if a is None:
            return Response({
                "status": "not_generated",
                "detail": "No assessment has been generated for this deal yet.",
            })

        coverage = float(a.input_coverage_pct or 0)
        categories = _build_scorecard(a)

        payload = {
            "assessmentId": str(a.id),
            "status": a.status,
            "configVersion": a.config_version,
            "dealStage": a.deal_stage,
            "stageWasDefaulted": a.stage_was_defaulted,
            # Phase 4: an unconfirmed stage is not a footnote. Every numeric
            # band in the scorecard below depends on it, so the client can
            # gate the headline on this rather than on a caveat nobody reads.
            "stageConfirmed": bool(a.deal_stage and not a.stage_was_defaulted),
            "sector": a.sector,
            "subSector": a.sub_sector,
            "assessmentDate": a.assessment_date,
            "createdAt": a.created_at,
            "coveragePct": coverage,
            "coverageFloorPct": COVERAGE_FLOOR_PCT,
            "auditStatus": a.audit_status,
            "categories": categories,
            # Phase 3: where the evidence came from. A scorecard built mostly
            # on researched public data is a different claim from one built
            # on the founder's own documents, and the reader should be able
            # to see which one they are holding.
            "evidenceMix": self._evidence_mix(a),
            "openReviewCount": a.review_log.filter(status="open").count(),
        }

        if coverage < COVERAGE_FLOOR_PCT:
            payload.update({
                "scoreSuppressed": True,
                "overallScore": None,
                "ratingBand": None,
                "suppressionReason": (
                    f"Not enough evidence to score this deal — {coverage:.0f}% "
                    f"of weighted parameters have supporting evidence, against "
                    f"a {COVERAGE_FLOOR_PCT}% floor."),
                "completeCategories": [c["code"] for c in categories
                                       if c["score"] is not None],
                "missingEvidence": self._missing_evidence(a),
            })
        else:
            payload.update({
                "scoreSuppressed": False,
                "overallScore": (float(a.overall_score)
                                 if a.overall_score is not None else None),
                "ratingBand": a.rating_band,
            })
        return Response(payload)

    @staticmethod
    def _evidence_mix(assessment):
        """Count the scored rows by where their evidence came from."""
        mix = {}
        for pv in assessment.parameter_values.exclude(score__isnull=True):
            key = pv.source_type or "unknown"
            mix[key] = mix.get(key, 0) + 1
        return [{"sourceType": k, "count": v}
                for k, v in sorted(mix.items(), key=lambda i: -i[1])]

    @staticmethod
    def _missing_evidence(assessment):
        """Unscored parameters grouped by the source that carries them,
        ordered by the weight each would recover.

        Naming the document is the point. "Coverage is low" is not actionable;
        "the financial model carries 20% of the score" is a task.
        """
        tenant_id = getattr(assessment, "tenant_id", None)
        have = set(assessment.parameter_values.exclude(score__isnull=True)
                   .values_list("input_key", flat=True))
        weights = _weights()
        by_source = {}
        from fundos.assessment.parameter_sources import source_for
        for key, p in _param_config(tenant_id).items():
            if not p.feeds_score or key in have:
                continue
            w = weights.get(("child", p.ref_code))
            # Named from the declared source of record. This previously read
            # an `expected_source` field that does not exist on the model, so
            # every gap fell into one bucket called "Additional information
            # required" — which is the sentence this block exists to replace.
            source = source_for(p)
            entry = by_source.setdefault(
                source, {"document": source, "weight": 0.0, "parameters": []})
            entry["weight"] += float(w.weight) if w else 0.0
            if len(entry["parameters"]) < 6:
                entry["parameters"].append(
                    _cfg_attr(p, "label", "name", default=key))
        for e in by_source.values():
            e["weight"] = round(e["weight"], 2)
        return sorted(by_source.values(), key=lambda e: -e["weight"])[:8]


class AssessmentGenerateView(DealScopedAPIView):
    """Extract evidence, then score. Returns a pollable job handle."""

    permission_classes = [IsAuthenticated]
    required_action = "readiness.generate"

    def post(self, request, deal_id):
        from fundos.assessment.tasks import generate_assessment_task
        return queue_generation(self, request, "assessment",
                                generate_assessment_task)


class ParameterDetailView(DealScopedAPIView):
    """The reasoning drawer.

    Section order is reasoning → rubric → evidence → override, deliberately:
    that is the order the argument has to be made in when a founder disputes a
    score. Here is what we concluded, here is the published rule, here is the
    evidence it was applied to, here is how to change it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id, key):
        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "No active assessment."},
                            status=status.HTTP_404_NOT_FOUND)
        pv = ParameterValue.objects.filter(assessment=a, input_key=key).first()
        if pv is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": f"Parameter {key} not found."},
                            status=status.HTTP_404_NOT_FOUND)

        tenant_id = getattr(a, "tenant_id", None)
        cfg = _param_config(tenant_id).get(key)
        w = _weights().get(("child", pv.ref_code))

        row = _serialise_value(pv, cfg, w.weight if w else None)
        if not row["label"]:
            row["label"] = pv.ref_code or key

        return Response({
            **row,
            "reasoning": pv.justification or "",
            "notes": pv.notes or "",
            "rubric": self._rubric(a, pv),
            "evidence": {
                "sourceType": pv.source_type or "",
                "sourceDetail": pv.source_detail or "",
                "sourceTier": pv.source_tier,
                "rawValue": pv.raw_value,
                "unit": pv.unit,
                "confidence": (float(pv.confidence)
                               if pv.confidence is not None else None),
            },
            "redistribution": self._redistribution(a, pv, cfg),
        })

    @staticmethod
    def _rubric(assessment, pv):
        """The published rule AT THE VERSION THAT WAS APPLIED.

        Reading live config would be wrong — a threshold may have changed since
        this assessment ran, and the drawer must show the rule that actually
        produced the score on screen.
        """
        version = assessment.config_version
        r = (ConfigRubric.objects.filter(input_key=pv.input_key,
                                         version=version)
             .filter(deal_stage=assessment.deal_stage)
             .order_by("tenant_id").last())
        if r:
            return {
                "type": "numeric", "version": version,
                "stage": assessment.deal_stage,
                "unit": _cfg_attr(r, "unit", default=pv.unit),
                "direction": _cfg_attr(r, "direction",
                                       default="higher_is_better"),
                "anchors": [
                    {"band": b, "label": _label(b), "score": s,
                     "threshold": _fmt(_cfg_attr(r, b, f"{b}_min",
                                                 default=None))}
                    for b, s in (("excellent", 9.0), ("good", 7.0),
                                 ("fair", 5.0), ("poor", 3.0))
                ],
                "note": "Where evidence is unclear, the band BELOW is applied.",
            }

        anc = (ConfigAnchor.objects.filter(input_key=pv.input_key,
                                           version=version)
               .order_by("tenant_id").last())
        if anc:
            return {
                "type": "qualitative", "version": version,
                "anchors": [
                    {"band": b, "label": _label(b), "score": s,
                     "definition": _cfg_attr(anc, b, f"{b}_text", default="")}
                    for b, s in (("excellent", 9.0), ("good", 7.0),
                                 ("fair", 5.0), ("poor", 3.0))
                ],
                "note": "Where evidence is unclear, the band BELOW is applied.",
            }
        return None

    @staticmethod
    def _redistribution(assessment, pv, cfg):
        """If this row contributed nothing, name the siblings that absorbed it."""
        if (pv.score is not None and not pv.is_reference_only) or cfg is None:
            return None
        parent = cfg.parent_code or cfg.category_code
        tenant_id = getattr(assessment, "tenant_id", None)
        weights = _weights()
        values = {v.input_key: v for v in assessment.parameter_values.all()}
        siblings = []
        for k, p in _param_config(tenant_id).items():
            if k == pv.input_key or not p.feeds_score:
                continue
            if (p.parent_code or p.category_code) != parent:
                continue
            sv = values.get(k)
            if sv is None or sv.score is None:
                continue
            w = weights.get(("child", p.ref_code))
            siblings.append({
                "label": _cfg_attr(p, "label", "name", default=k),
                "weight": float(w.weight) if w else None})
        return {
            "appliedWeight": 0.0,
            "absorbedBy": siblings,
            "explanation": (
                "This parameter could not be assessed, so its weight was "
                "redistributed proportionally across its siblings. The "
                "sub-category still totals 100%."),
        }


def _fmt(value):
    return "—" if value in (None, "") else str(value)


class ParameterOverrideView(DealScopedAPIView):
    """Apply an override and re-roll the totals.

    The system score is never destroyed — `system_band` and `system_score` are
    separate columns the engine writes on every run. That separation is what
    keeps an overridden scorecard auditable.
    """

    permission_classes = [IsAuthenticated]
    required_action = "strategy.edit"

    def post(self, request, deal_id, key):
        from fundos.assessment.services import apply_parameter_override

        raw = request.data.get("score")
        reason = (request.data.get("reason") or "").strip()
        if raw is None:
            return Response({"error": "E-VALIDATION-400",
                             "detail": "score is required."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not reason:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "A reason is required for every override.",
                 "fields": {"reason": "Explain why the system score is wrong."}},
                status=status.HTTP_400_BAD_REQUEST)
        try:
            score = float(raw)
        except (TypeError, ValueError):
            return Response({"error": "E-VALIDATION-400",
                             "detail": "score must be a number."},
                            status=status.HTTP_400_BAD_REQUEST)
        if not 0 <= score <= 10:
            return Response({"error": "E-VALIDATION-400",
                             "detail": "score must be between 0 and 10."},
                            status=status.HTTP_400_BAD_REQUEST)

        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404"},
                            status=status.HTTP_404_NOT_FOUND)
        pv = ParameterValue.objects.filter(assessment=a, input_key=key).first()
        if pv is None:
            return Response({"error": "E-NOTFOUND-404"},
                            status=status.HTTP_404_NOT_FOUND)

        # The write, the review-trail entry and the re-roll all live in
        # `services.apply_parameter_override`, because the Phase 1 scorecard
        # offers the same action and two copies of the cascade would be two
        # chances for the audit trail to be written only one of them.
        a = apply_parameter_override(a, pv, score=score, reason=reason,
                                     user=request.user)

        return Response({
            "status": "applied",
            "overallScore": (float(a.overall_score)
                             if a.overall_score is not None else None),
            "ratingBand": a.rating_band,
            "coveragePct": float(a.input_coverage_pct or 0),
        })


class AssessmentSuggestionsView(DealScopedAPIView):
    """GET — what is worth asking about a node of the scorecard.

    `?ref=` addresses any level: a category (A), a child (A.1) or the row
    that actually scores (A.1.d). Each level gets different questions,
    because they ARE different questions -- nothing settles a category, its
    rows do. With no ref, the six categories come back, which is what a
    panel that has not drilled in yet needs.

    Computed from the scorecard, not asked of a model: the anchor already
    defines what each band requires, and the engine can say exactly what a
    change is worth. Instant, free, and unable to name a row that does not
    exist.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        from fundos.assessment import suggestions

        a = _active(self.deal)
        if a is None:
            return Response({"suggestions": {}, "assessed": False})

        raw = (request.query_params.get("ref") or "").strip()
        refs = [r.strip() for r in raw.split(",") if r.strip()] or None
        return Response({"assessed": True,
                         "suggestions": suggestions.for_assessment(a, refs)})


class AssessmentQAView(DealScopedAPIView):
    """POST — a question about this scorecard, and any override it proposes.

    The chat never changes a score. It may propose one; a person applies it
    through `…/parameters/{key}/override`, which demands a reason and keeps
    the machine's own answer in its own columns.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, deal_id):
        from fundos.assessment.qa import answer

        question = ((request.data or {}).get("question") or "").strip()
        if not question:
            return Response({"detail": "question is required"}, status=400)

        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "This deal has no assessment yet."},
                            status=status.HTTP_404_NOT_FOUND)

        return Response(answer(a, question,
                               ref=((request.data or {}).get("ref") or ""),
                               user=request.user))


class DiligenceFindingsView(DealScopedAPIView):
    """Extracted evidence, observations, and the question to ask the founder.

    Integrity findings and the AI rubric review's flags land in the same table.
    From the reader's point of view they are all "things about this deal that
    need a human", and splitting them across two screens means one goes unread.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        a = _active(self.deal)
        if a is None:
            return Response({"items": [], "total": 0})

        items = []
        for pv in a.parameter_values.filter(band__in=["poor", "fair"]):
            items.append({
                "category": pv.category,
                "parameter": pv.ref_code or pv.input_key,
                "evidence": pv.source_detail or "",
                "observation": pv.justification or "",
                "ask": pv.notes or "",
                "band": pv.band,
                "severity": "high" if pv.band == "poor" else "medium",
                "source": "scorecard",
            })

        for f in (a.audit_findings or []):
            items.append({
                "category": f.get("category", ""),
                "parameter": f.get("parameter") or f.get("code", ""),
                "evidence": f.get("evidence", ""),
                "observation": f.get("message") or f.get("observation", ""),
                "ask": f.get("ask", ""),
                "band": "",
                "severity": f.get("severity", "warning"),
                "source": f.get("source", "integrity"),
            })

        order = {"high": 0, "error": 0, "medium": 1, "warning": 1,
                 "low": 2, "info": 2}
        items.sort(key=lambda i: order.get(i["severity"], 3))
        return Response({"items": items, "total": len(items)})


class BandAdvancementView(DealScopedAPIView):
    """What would move you up a band, ordered by weighted impact.

    Ordering by impact rather than by category is what makes this a to-do list
    instead of advice: the parameter that moves the overall number most comes
    first, with the delta stated so effort can be judged against it.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        a = _active(self.deal)
        if a is None:
            return Response({"items": []})

        tenant_id = getattr(a, "tenant_id", None)
        params = _param_config(tenant_id)
        weights = _weights()

        items = []
        for pv in a.parameter_values.all():
            nxt = NEXT_BAND.get(pv.band)
            if not nxt or pv.score is None or pv.is_reference_only:
                continue
            target_band, target_score = nxt
            cfg = params.get(pv.input_key)
            w = weights.get(("child", pv.ref_code))
            weight = float(w.weight) if w else 0.0
            items.append({
                "key": pv.input_key,
                "parameter": (_cfg_attr(cfg, "label", "name", default="")
                              if cfg else "") or pv.ref_code or pv.input_key,
                "category": pv.category,
                "currentBand": pv.band,
                "currentBandLabel": _label(pv.band),
                "currentScore": float(pv.score),
                "targetBand": target_band,
                "targetBandLabel": _label(target_band),
                "requiredProof": self._proof(a, pv, target_band),
                "overallImpact": round(
                    (target_score - float(pv.score)) * weight / 100.0, 2),
            })

        items.sort(key=lambda i: -i["overallImpact"])
        return Response({"items": items[:20]})

    @staticmethod
    def _proof(assessment, pv, target_band):
        """Quote the published anchor for the target band.

        The proof point IS the rule. Inventing advice here would mean making up
        a threshold, which is exactly what the rubric exists to prevent.
        """
        version = assessment.config_version
        r = (ConfigRubric.objects.filter(input_key=pv.input_key,
                                         version=version,
                                         deal_stage=assessment.deal_stage)
             .order_by("tenant_id").last())
        if r:
            threshold = _cfg_attr(r, target_band, f"{target_band}_min",
                                  default=None)
            if threshold not in (None, ""):
                unit = _cfg_attr(r, "unit", default=pv.unit)
                return f"Reach {threshold}{(' ' + unit) if unit else ''}."
        anc = (ConfigAnchor.objects.filter(input_key=pv.input_key,
                                           version=version)
               .order_by("tenant_id").last())
        if anc:
            return _cfg_attr(anc, target_band, f"{target_band}_text", default="")
        return ""


class AssessmentVersionsView(DealScopedAPIView):
    """Version list and side-by-side diff (D-10).

    The schema already supported this — `superseded_by` and `status` were there
    from the start. Only the comparison was missing.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        rows = Assessment.objects.filter(deal=self.deal).order_by("-created_at")
        items = [{
            "assessmentId": str(x.id),
            "status": x.status,
            "overallScore": (float(x.overall_score)
                             if x.overall_score is not None else None),
            "ratingBand": x.rating_band,
            "coveragePct": float(x.input_coverage_pct or 0),
            "configVersion": x.config_version,
            "dealStage": x.deal_stage,
            "createdAt": x.created_at,
        } for x in rows]

        a_id = request.query_params.get("a")
        b_id = request.query_params.get("b")
        diff = None
        if a_id and b_id:
            diff = self._diff(a_id, b_id)
        elif len(items) >= 2:
            diff = self._diff(items[0]["assessmentId"],
                              items[1]["assessmentId"])
        return Response({"items": items, "diff": diff})

    @staticmethod
    def _diff(a_id, b_id):
        try:
            a = Assessment.objects.get(id=a_id)
            b = Assessment.objects.get(id=b_id)
        except (Assessment.DoesNotExist, ValueError, TypeError):
            return None

        va = {p.input_key: p for p in a.parameter_values.all()}
        vb = {p.input_key: p for p in b.parameter_values.all()}
        changes = []
        for key in set(va) | set(vb):
            pa, pb = va.get(key), vb.get(key)
            sa = float(pa.score) if pa and pa.score is not None else None
            sb = float(pb.score) if pb and pb.score is not None else None
            if sa == sb:
                continue
            changes.append({
                "key": key,
                "label": (pa or pb).ref_code or key,
                "from": sb, "to": sa,
                "fromBand": pb.band if pb else None,
                "toBand": pa.band if pa else None,
                "delta": round((sa or 0) - (sb or 0), 2),
            })
        changes.sort(key=lambda c: -abs(c["delta"]))
        return {
            "a": {"id": str(a.id), "createdAt": a.created_at,
                  "overallScore": (float(a.overall_score)
                                   if a.overall_score is not None else None)},
            "b": {"id": str(b.id), "createdAt": b.created_at,
                  "overallScore": (float(b.overall_score)
                                   if b.overall_score is not None else None)},
            "changes": changes[:50],
        }


# ===========================================================================
# v23 Phase 4 — stage gating, coverage shape, and the review log
# ===========================================================================

class AssessmentStageView(DealScopedAPIView):
    """Read the stage options, or set the stage and re-score.

    WHY THIS IS ITS OWN ENDPOINT AND NOT A FIELD ON A PATCH
    -------------------------------------------------------
    Stage is not one input among eighty. It selects the cut-point column for
    every numeric parameter in the model (§2.5), so changing it re-bands the
    entire scorecard — the same 14-month runway is Fair at Series A and Good
    at Growth. Setting it therefore re-scores immediately and says what moved,
    rather than leaving a scorecard on screen that no longer matches the stage
    beside it.

    GET also reports whether the current stage was DEFAULTED. The engine has
    always flagged that internally; until now nothing asked it, so a scorecard
    silently banded against Series A cut-points looked identical to one a
    human had confirmed.
    """

    permission_classes = [IsAuthenticated]
    required_action = "strategy.edit"

    def get(self, request, deal_id):
        from fundos.assessment.models import DEAL_STAGES

        a = _active(self.deal)
        return Response({
            "options": [{"value": v, "label": label}
                        for v, label in DEAL_STAGES],
            "dealStage": a.deal_stage if a else "",
            "stageWasDefaulted": bool(a.stage_was_defaulted) if a else None,
            "isConfirmed": bool(a and a.deal_stage and
                                not a.stage_was_defaulted),
            "why": ("Stage selects the cut-points every numeric parameter is "
                    "banded against. Confirm it before relying on the score."),
        })

    def post(self, request, deal_id):
        from fundos.assessment.models import DEAL_STAGES
        from fundos.assessment.services import run_scoring

        requested = (request.data.get("dealStage")
                     or request.data.get("stage") or "").strip()
        valid = {v for v, _ in DEAL_STAGES}
        if requested not in valid:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": f"dealStage must be one of {sorted(valid)}.",
                 "fields": {"dealStage": "Choose a funding stage."}},
                status=status.HTTP_400_BAD_REQUEST)

        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "No active assessment."},
                            status=status.HTTP_404_NOT_FOUND)

        before_score = (float(a.overall_score)
                        if a.overall_score is not None else None)
        before_band = a.rating_band
        before_stage = a.deal_stage

        a.deal_stage = requested
        # An explicit choice is by definition not a default, and the audit
        # warning that goes with it must clear at the same moment.
        a.stage_was_defaulted = False
        a.save(update_fields=["deal_stage", "stage_was_defaulted"])

        run_scoring(a, user=request.user)
        a.refresh_from_db()

        logger.info("ASSESSMENT %s: stage %s → %s by %s", a.id, before_stage,
                    requested, getattr(request.user, "email", "?"))
        return Response({
            "status": "applied",
            "dealStage": a.deal_stage,
            "previousStage": before_stage,
            "overallScore": (float(a.overall_score)
                             if a.overall_score is not None else None),
            "previousScore": before_score,
            "ratingBand": a.rating_band,
            "previousRatingBand": before_band,
            "coveragePct": float(a.input_coverage_pct or 0),
            "auditStatus": a.audit_status,
        })


class AssessmentCoverageView(DealScopedAPIView):
    """Where the evidence gaps actually are.

    The headline coverage number tells a founder that something is missing;
    this tells them what and where. 60% overall can mean every category
    two-thirds answered, or four complete and Financials entirely empty —
    different deals needing different next actions.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, deal_id):
        from fundos.assessment.services import coverage_by_category

        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "No active assessment."},
                            status=status.HTTP_404_NOT_FOUND)

        rows = coverage_by_category(a)
        labels = {w.code: w.label for w in ConfigWeight.objects.filter(
            level="category", is_active=True)}
        weights = {w.code: float(w.weight)
                   for w in ConfigWeight.objects.filter(
                       level="category", is_active=True)}
        for r in rows:
            r["label"] = labels.get(r["categoryCode"], "")
            r["weight"] = weights.get(r["categoryCode"], 0.0)
            # The share of the RATING this gap represents, not the share of
            # the parameter count. A blank in Sub-Sector costs twice what a
            # blank in Business Quality does, and the ordering must say so.
            r["unevidencedWeight"] = round(
                r["weight"] * (100 - r["coveragePct"]) / 100, 2)

        return Response({
            "coveragePct": float(a.input_coverage_pct or 0),
            "coverageFloorPct": COVERAGE_FLOOR_PCT,
            "scoreSuppressed": float(a.input_coverage_pct or 0) < COVERAGE_FLOOR_PCT,
            "categories": rows,
            "byPriority": sorted(rows, key=lambda r: -r["unevidencedWeight"]),
        })


class ReviewLogView(DealScopedAPIView):
    """The founder pushback trail (§6.1, Assessment Control §D).

    The table existed from the first migration and nothing ever wrote to it.
    That is the gap this closes, and it matters more than it looks: a founder
    disagreeing with a score is information about the MODEL, not just about
    the deal. A rejected challenge needs a record as much as an accepted one,
    because the pattern of what gets rejected is how a bad cut-point is found.
    """

    permission_classes = [IsAuthenticated]
    required_action = "strategy.edit"

    def get(self, request, deal_id):
        from fundos.assessment.models import ReviewLog

        a = _active(self.deal)
        if a is None:
            return Response({"items": [], "openCount": 0})
        rows = ReviewLog.objects.filter(assessment=a)
        state = request.query_params.get("status")
        if state:
            rows = rows.filter(status=state)
        return Response({
            "items": [_serialise_review(r) for r in rows],
            "openCount": ReviewLog.objects.filter(assessment=a,
                                                  status="open").count(),
        })

    def post(self, request, deal_id):
        """Raise a challenge against a parameter or a topic."""
        from fundos.assessment.models import ReviewLog

        a = _active(self.deal)
        if a is None:
            return Response({"error": "E-NOTFOUND-404",
                             "detail": "No active assessment."},
                            status=status.HTTP_404_NOT_FOUND)

        topic = (request.data.get("fieldOrTopic") or "").strip()
        reason = (request.data.get("founderReason") or "").strip()
        if not topic:
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "fieldOrTopic is required.",
                 "fields": {"fieldOrTopic": "Name the parameter or topic."}},
                status=status.HTTP_400_BAD_REQUEST)
        if not reason:
            # A challenge with no reason cannot be actioned or learned from,
            # and would sit open forever as noise in the trail.
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "founderReason is required.",
                 "fields": {"founderReason": "Explain what is wrong."}},
                status=status.HTTP_400_BAD_REQUEST)

        previous = ""
        pv = ParameterValue.objects.filter(assessment=a,
                                           input_key=topic).first()
        if pv is not None:
            previous = f"{pv.raw_value} [{pv.band or 'unassessed'}]"

        row = ReviewLog.objects.create(
            assessment=a, field_or_topic=topic[:128], previous_value=previous,
            founder_value=(request.data.get("founderValue") or "")[:2000],
            founder_reason=reason[:2000], raised_by=request.user,
            status="open")
        logger.info("ASSESSMENT %s: review raised on %s by %s", a.id, topic,
                    getattr(request.user, "email", "?"))
        return Response(_serialise_review(row), status=201)


class ReviewLogEntryView(DealScopedAPIView):
    """Close or reject one challenge, with the action recorded."""

    permission_classes = [IsAuthenticated]
    required_action = "strategy.edit"

    def patch(self, request, deal_id, entry_id):
        from fundos.assessment.models import ReviewLog

        a = _active(self.deal)
        row = ReviewLog.objects.filter(id=entry_id, assessment=a).first()
        if row is None:
            return Response({"error": "E-NOTFOUND-404"},
                            status=status.HTTP_404_NOT_FOUND)

        new_status = (request.data.get("status") or "").strip()
        if new_status not in ("open", "closed", "rejected"):
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "status must be open, closed or rejected."},
                status=status.HTTP_400_BAD_REQUEST)

        action = (request.data.get("agentAction") or "").strip()
        if new_status in ("closed", "rejected") and not action:
            # Rejecting silently is the failure mode this table exists to
            # prevent: the founder learns nothing and neither does the model.
            return Response(
                {"error": "E-VALIDATION-400",
                 "detail": "agentAction is required when closing or "
                           "rejecting a challenge.",
                 "fields": {"agentAction": "Say what was done and why."}},
                status=status.HTTP_400_BAD_REQUEST)

        row.status = new_status
        row.agent_action = action[:2000]
        row.save(update_fields=["status", "agent_action"])
        return Response(_serialise_review(row))


def _serialise_review(row):
    return {
        "id": str(row.id),
        "fieldOrTopic": row.field_or_topic,
        "previousValue": row.previous_value,
        "founderValue": row.founder_value,
        "founderReason": row.founder_reason,
        "agentAction": row.agent_action,
        "status": row.status,
        "raisedBy": getattr(row.raised_by, "email", None),
        "createdAt": row.created_at,
    }
