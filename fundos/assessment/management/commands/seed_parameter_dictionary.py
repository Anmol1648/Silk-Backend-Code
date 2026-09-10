"""Backfill the workbook's data dictionary onto the config parameters.

Why this is needed
------------------
`ConfigParameter` carries three prose fields the extraction prompt is built
from: ``definition`` (what the parameter actually measures), ``where_to_find``
(which document and which line item), and ``if_missing`` (what to do when the
pack does not carry it). They are the difference between asking a model for
"TEAM_FDR_EXP" and asking it for "years of full-time work in this industry,
counted from the earliest verifiable role in the sector, taken from the founder
CV rather than inferred from age".

``seed_assessment_config`` ships none of them -- `spec_config_data.py` has no
definition field at all -- so a fresh install prompts on bare keys. They arrive
only through ``import_assessment_workbook``, which needs the source .xlsx. An
environment stood up without that workbook extracts against a parameter list
with no definitions, no guidance on where to look, and no instruction on what
absence means, and the coverage figure shows it.

The same dictionary already ships in this repository, as reference data the V2
serializer reads at request time to render each parameter's `dictionary` block.
This command loads it into the config rows so the extraction prompt can be
built from it too, rather than the same text being available for display and
absent where it would change the answer.

Joining the two
---------------
The dictionary is keyed by ref code (``A.1.a``), and ref codes are NOT unique
in this schema: sector mirrors put two inputs on E.1, and every cross-check row
shares its ref with the parameter it checks. That is not an ambiguity to be
avoided, though -- two inputs on one ref ARE one parameter, the row that scores
it and the row that cross-checks it, and the dictionary describes the pair. So
the join reads every input key named in ``rubric_or_anchor`` (which states them
in several shapes: "TEAM_MARQUEE_CLIENTS / ANC_FDR_NETWORK", "ANC_MOAT_IP (ref
BQ_PATENTS)", "DD_DIRECT_APPROACHES (ANC_FDR_BANKER)") and falls back to the
ref code, which reaches both siblings with the prose that describes them both.

Nothing already populated is overwritten unless ``--overwrite`` is passed: an
operator who has edited a definition by hand outranks a shipped default.
"""
import json
import os
import re

from django.core.management.base import BaseCommand
from django.db import transaction

DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "parameter_dictionary.json")

FIELDS = ("definition", "where_to_find", "if_missing")


KEY_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{3,}")


def _by_input_key(payload):
    """Dictionary entries indexed by every input key they name.

    ``rubric_or_anchor`` states its keys in several shapes -- one key, two
    separated by a slash, or one with the other in parentheses -- so the keys
    are read out by pattern rather than by splitting on a separator that only
    covers one of them. Every key named describes the same parameter, so each
    gets the same prose.
    """
    out = {}
    for entry in payload.values():
        for key in KEY_TOKEN.findall(str(entry.get("rubric_or_anchor") or "")):
            out.setdefault(key, entry)
    return out


def _by_ref(payload):
    """Entries indexed by ref code, dropping any the codes cannot separate."""
    return {ref: entry for ref, entry in payload.items() if ref}


class Command(BaseCommand):
    help = ("Backfill definition, where_to_find and if_missing onto the "
            "active config parameters from the shipped data dictionary. The "
            "extraction prompt is built from these fields.")

    def add_arguments(self, parser):
        parser.add_argument("--path", default=DATA_FILE)
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Replace text already on a row. Off by default: an operator "
                 "who edited a definition by hand outranks a shipped one.")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        from fundos.assessment.models import ConfigParameter

        path = options["path"]
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f"No dictionary at {path}."))
            raise SystemExit(1)

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)

        by_key = _by_input_key(payload)
        by_ref = _by_ref(payload)

        params = list(ConfigParameter.objects.filter(is_active=True))

        filled, skipped, unmatched = 0, 0, []
        with transaction.atomic():
            for param in params:
                entry = by_key.get(param.input_key) or by_ref.get(
                    param.ref_code)
                if entry is None:
                    unmatched.append(param.input_key)
                    continue

                changed = []
                for field in FIELDS:
                    text = str(entry.get(field) or "").strip()
                    if not text:
                        continue
                    if getattr(param, field) and not options["overwrite"]:
                        continue
                    if getattr(param, field) == text:
                        continue
                    changed.append(field)
                    if not options["dry_run"]:
                        setattr(param, field, text)
                if changed:
                    filled += 1
                    if not options["dry_run"]:
                        param.save(update_fields=changed)
                else:
                    skipped += 1

            if options["dry_run"]:
                transaction.set_rollback(True)

        verb = "would fill" if options["dry_run"] else "filled"
        self.stdout.write(
            f"{verb} {filled} parameter(s); {skipped} already complete")
        if unmatched:
            self.stdout.write(
                f"  {len(unmatched)} row(s) have no dictionary entry: "
                f"{', '.join(sorted(unmatched)[:8])}")
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing written."))
