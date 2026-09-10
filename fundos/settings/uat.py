"""UAT settings — hardened deltas only (Doc 8 §2)."""
from .base import *  # noqa
from .base import env

DEBUG = False

# Signing keys are required here for the same reason as in prod: UAT holds
# real user accounts and real documents, and `base`'s development fallback for
# SECRET_KEY (which FUNDOS_JWT_SECRET inherits) is a value printed in this
# repository. A missing variable must stop the process, not silently make
# every token forgeable.
SECRET_KEY = env("FUNDOS_SECRET_KEY", required=True)
FUNDOS_JWT_SECRET = env("FUNDOS_JWT_SECRET", required=True)

# Issues 9/20: default AI mode for a fresh seed in this environment.
FUNDOS_AI_MOCKED_DEFAULT = False
