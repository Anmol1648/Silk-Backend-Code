"""
python manage.py diagnose_profile --company "Terracotta Living"

Prints a full, human-readable diagnosis of why a company's profile is empty
or thin. Run it against a live system and send the output to whoever is
investigating — it contains the configuration state, the source availability,
the AI call outcome and a plain-language cause and fix.

    --company   name (partial match) or id
    --generate  actually run generation and trace it live (costs a call)
    --json      machine-readable output
"""
import json

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Diagnose why a company profile is empty or thin."

    def add_arguments(self, parser):
        parser.add_argument("--company", required=True,
                            help="Company name (partial) or id")
        parser.add_argument("--generate", action="store_true",
                            help="Run generation now and trace it live")
        parser.add_argument("--json", action="store_true",
                            help="Emit JSON instead of a report")

    def handle(self, *args, **opts):
        from fundos.core.models import Company
        from fundos.profile.models import ProfileGenerationRun
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile,
                                             preflight_report)
        from fundos.profile.trace import GenerationTrace

        term = opts["company"]
        company = Company.objects.filter(name__icontains=term).first()
        if company is None:
            try:
                company = Company.objects.filter(id=term).first()
            except Exception:
                company = None
        if company is None:
            raise CommandError(f"No company matching {term!r}.")

        profile = get_or_create_profile(company)

        if opts["generate"]:
            trace = GenerationTrace(profile=profile, company=company,
                                    mode="diagnostic", trigger="manual")
            with trace:
                generate_profile(profile, force=True)
            payload = trace.to_dict()
            report = trace.report()
        else:
            # Report on the LAST recorded run, plus the CURRENT configuration.
            run = (ProfileGenerationRun.objects
                   .filter(profile=profile).order_by("-created_at").first())
            trace = GenerationTrace(profile=profile, company=company,
                                    mode="inspection", trigger="manual")
            cfg = preflight_report(profile)
            trace.event("preflight.config", "OK", **{
                k: v for k, v in cfg.items()})
            stored = ((run.diagnostics or {}).get("trace")
                      if run and isinstance(run.diagnostics, dict) else None)
            if stored:
                trace.steps.extend(stored.get("steps", []))
            else:
                trace.event(
                    "history.none", "WARN",
                    note=("no traced generation run is recorded for this "
                          "company — re-run with --generate to produce one"))
            self._section_state(trace, profile)
            payload = trace.to_dict()
            report = trace.report()

        if opts["json"]:
            self.stdout.write(json.dumps(payload, indent=2, default=str))
        else:
            self.stdout.write(report)

    def _section_state(self, trace, profile):
        """Record which sections currently hold data — the visible symptom."""
        from fundos.profile.spec_serializer import build_sections
        from fundos.profile.trace import OK, WARN

        try:
            sections = build_sections(profile)
        except Exception as e:
            trace.event("sections.inspect", "FAIL", error=str(e))
            return
        empty = [k for k, v in sections.items() if not v.get("isComplete")]
        trace.event("sections.state", WARN if len(empty) == len(sections) else OK,
                    total=len(sections),
                    populated=len(sections) - len(empty),
                    empty=len(empty),
                    empty_sections=",".join(sorted(empty)[:12]))
