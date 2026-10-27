"""Scientific source ingestion and normalization."""


class PaperSourceError(RuntimeError):
    """A paper source cannot be resolved or prepared for poster authoring."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
