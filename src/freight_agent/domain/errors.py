"""Domain errors."""


class DomainError(Exception):
    """A rule of the domain was broken or input could not be understood."""


class InvariantViolation(DomainError):
    """An operation would leave an entity in an invalid state."""
