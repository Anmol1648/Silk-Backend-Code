from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.dispatch import receiver


@receiver(connection_created)
def _tune_sqlite(sender, connection, **kwargs):
    """Let SQLite handle a reader and a writer at the same time.

    WHY THIS EXISTS
    ---------------
    A profile generation run holds a write connection for minutes. In SQLite's
    default rollback-journal mode ONE writer locks the whole database, and a
    second connection that cannot get the lock raises `OperationalError:
    database is locked` — immediately, because Django's default timeout for
    the sqlite backend is 5 seconds and the lock is not going to clear inside
    it anyway.

    That is exactly how a Deal Scorecard came back empty: the Phase 1 GET
    kicked off assessment generation seconds after a profile run, the
    generation job failed in 0.14s with "database is locked", and the screen
    had nothing to render. The failure was invisible on the page and looked
    like missing data rather than a blocked write.

    Two settings fix it, and both are per-connection rather than per-file:

      journal_mode=WAL   Readers no longer block the writer and the writer no
                         longer blocks readers. Writers still serialise
                         against each other, which is correct.
      busy_timeout       Wait for a contended lock instead of failing at
                         once. A background job that takes a moment longer is
                         always better than one that fails and leaves a blank
                         screen.

    Postgres does all of this natively, so this is scoped to sqlite and is a
    no-op everywhere else.
    """
    if connection.vendor != "sqlite":
        return
    timeout_ms = int(
        (connection.settings_dict.get("OPTIONS") or {}).get("timeout", 30)
    ) * 1000
    with connection.cursor() as cursor:
        # WAL is a property of the database FILE and persists once set, but it
        # is re-asserted here so a fresh checkout or a restored copy gets it
        # without anyone having to remember.
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms};")
        # NORMAL is the durability level WAL is designed around: safe against
        # process crashes, and only at risk from an OS-level crash mid-write.
        cursor.execute("PRAGMA synchronous=NORMAL;")


class CoreConfig(AppConfig):
    name = "fundos.core"
    label = "core"
    verbose_name = "FundOS Core (M0)"
