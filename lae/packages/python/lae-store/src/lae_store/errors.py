class StoreError(RuntimeError):
    """Base class for stable persistence-boundary failures."""


class ResourceNotFound(StoreError):
    pass


class IdempotencyKeyReused(StoreError):
    pass


class OperationConflict(StoreError):
    pass


class SourceConnectionConflict(StoreError):
    pass


class SourceConnectionUnavailable(StoreError):
    pass


class SourceConnectionHostMismatch(StoreError):
    pass


class CredentialLeaseRejected(StoreError):
    """Stable, non-secret-bearing credential lease redemption failure."""

    pass


class InvalidOperationTransition(StoreError):
    pass


class LeaseLost(StoreError):
    pass


class ApplicationConflict(StoreError):
    pass


class ApplicationQuotaExceeded(StoreError):
    pass


class ApplicationAlreadyMaterialized(StoreError):
    pass


class SubscriptionUnavailable(StoreError):
    pass


class InvalidPlanLimits(StoreError):
    pass


class CustomDomainUnsupported(StoreError):
    pass


class EnvironmentVersionConflict(StoreError):
    def __init__(self, *, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"environment version conflict: expected {expected}, actual {actual}"
        )


class DeploymentConflict(StoreError):
    pass


class DeploymentQuotaExceeded(StoreError):
    pass


class DeploymentPlanInvalid(StoreError):
    pass


class DeploymentPlanUnavailable(StoreError):
    pass


class DeploymentTopologyConflict(StoreError):
    pass


class DeploymentEnvironmentIncomplete(StoreError):
    pass


class DeploymentEnvironmentScopeInvalid(StoreError):
    """Stored environment scopes are incompatible with the trusted plan."""

    pass


class DeploymentEnvironmentSchemaConflict(StoreError):
    """The caller configured a different verified environment schema."""

    pass


class ApplicationLifecycleConflict(StoreError):
    """The requested application transition conflicts with durable state."""

    pass


class ApplicationLifecycleStateConflict(StoreError):
    """The requested action is invalid for the application's desired state."""

    pass


class ApplicationLifecycleSourceUnavailable(StoreError):
    """No reusable, tenant-owned Git source is available for update checking."""

    pass


class ApplicationRollbackUnavailable(StoreError):
    """No verified previous deployment is available for rollback."""

    pass


class UploadUnavailable(StoreError):
    """The private upload object-store capability is not configured."""

    pass


class UploadQuotaExceeded(StoreError):
    pass


class UploadConflict(StoreError):
    pass


class UploadVerificationFailed(StoreError):
    """A stable, non-secret-bearing object verification failure."""

    pass
