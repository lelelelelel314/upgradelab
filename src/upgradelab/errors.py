"""Domain-specific failures for guarded execution."""


class UpgradeLabError(Exception):
    """Base class for UpgradeLab errors."""


class ConcurrencyConflict(UpgradeLabError):
    """A compare-and-swap update lost a race."""


class LeaseLost(UpgradeLabError):
    """A worker attempted to mutate a run with a stale lease."""


class InvalidTransition(UpgradeLabError):
    """A state transition violates the run state machine."""


class EffectConflict(UpgradeLabError):
    """An idempotency key was reused for a different operation request."""


class EffectNeedsReconciliation(UpgradeLabError):
    """An interrupted side effect must be reconciled before it can continue."""


class PatchPolicyViolation(UpgradeLabError):
    """A candidate patch crosses the configured repair boundary."""
