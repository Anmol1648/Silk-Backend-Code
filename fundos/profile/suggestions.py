"""What the chat should offer to ask, for each section that has a gap.

The Q&A box takes a typed question and offers nothing, so a founder looking
at a readiness ring has to invent the question that would move it. These are
the questions worth asking, derived from the SAME three numbers the readiness
breakdown already reports -- fields, populated, confirmed -- so a suggestion
can never describe a state the section is not actually in.

Computed here rather than asked of a model. Every suggestion below is a
template over data the request already holds: it costs nothing, it returns
instantly, and it cannot name a field that does not exist -- which a model
writing chat prompts from a profile would eventually do, and which is a
worse failure than no suggestions at all, because a founder would answer it.

A section that is fully populated and fully confirmed produces nothing. A
short, honest list is the point; a chip on every section regardless of state
is decoration that teaches people to ignore the row.
"""
from fundos.profile.labels import humanize, section_label

#: At most this many per section, so a profile at the start of its life
#: offers a readable list rather than one chip per blank.
MAX_PER_SECTION = 2

#: How many blank fields to name before saying "and N more". Naming them is
#: the whole value -- "2 fields missing" sends a founder hunting.
MAX_NAMED_FIELDS = 3


def _named(fields):
    """"Website, Country and 2 more" -- an English list, not a count."""
    shown = [humanize(f) for f in fields[:MAX_NAMED_FIELDS]]
    rest = len(fields) - len(shown)
    if not shown:
        return ""
    # Only ONE "and" in the sentence. Joining the names with "and" and then
    # appending "and N more" produced "A, B and C and 1 more", which reads
    # as four things where three are named.
    if rest:
        return ", ".join(shown) + f" and {rest} more"
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + f" and {shown[-1]}"


def for_section(key, *, fields, populated, confirmed, blank_fields=None):
    """The suggestions for one section, most useful first.

    :param fields: the section's denominator -- declared fields for an object
        section, item count for a list one.
    :param populated: how many of those carry a value.
    :param confirmed: how many a founder has confirmed.
    :param blank_fields: the names of the unpopulated fields where they are
        nameable. A list section's gap is a missing ROW, which has no name
        until it exists, so this is empty there and the suggestion says so.
    """
    name = section_label(key)
    blank_fields = list(blank_fields or [])
    out = []

    if not fields or not populated:
        out.append({
            "sectionKey": key,
            "kind": "empty",
            "label": f"Start {name}",
            "question": (f"What do you need from me to complete the {name} "
                         f"section, and where would that information "
                         f"normally come from?"),
            "why": f"{name} is empty",
        })
    elif populated < fields:
        missing = fields - populated
        listed = _named(blank_fields)
        out.append({
            "sectionKey": key,
            "kind": "fill",
            "label": (f"Fill in {listed}" if listed
                      else f"Fill {missing} blank in {name}"),
            "question": (
                f"In {name}, {listed} {'is' if len(blank_fields) == 1 else 'are'} "
                f"blank. What should go there, and what do you already know "
                f"from my documents that would answer it?"
                if listed else
                f"{missing} of the {fields} entries in {name} are blank. "
                f"Which are they, and what should go in them?"),
            "why": f"{missing} of {fields} blank",
        })

    if populated and confirmed < populated:
        unconfirmed = populated - confirmed
        out.append({
            "sectionKey": key,
            "kind": "confirm",
            "label": (f"Check {unconfirmed} unconfirmed in {name}"
                      if unconfirmed > 1 else f"Check {name} before I confirm"),
            "question": (
                f"Summarise what {name} currently says and where each part "
                f"came from, so I can check it before confirming."),
            "why": (f"{unconfirmed} of {populated} populated "
                    f"{'entry is' if unconfirmed == 1 else 'entries are'} "
                    f"unconfirmed"),
        })

    return out[:MAX_PER_SECTION]


def attach(breakdown):
    """Add `suggestions` to every breakdown row that has a gap.

    Mutates and returns the rows. A row with nothing to suggest carries an
    empty list rather than no key, so a client never has to test for absence.

    Called where the breakdown is SERVED, not where it is computed: the
    readiness score rebuilds the breakdown to divide by it and has no use for
    prose.
    """
    for row in breakdown:
        row["suggestions"] = for_section(
            row.get("sectionKey", ""),
            fields=row.get("fields") or 0,
            populated=row.get("populated") or 0,
            confirmed=row.get("confirmed") or 0,
            blank_fields=row.get("blankFields"))
    return breakdown
