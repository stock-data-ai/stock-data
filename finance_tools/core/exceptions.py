class StockDataError(Exception):
    """Base exception for all Stock Data project errors."""
    pass

class ApiError(StockDataError):
    """Base exception for API related errors."""
    pass

class ApiExhaustedError(ApiError):
    """Raised when API token quota is exhausted."""
    pass

class ApiResponseError(ApiError):
    """Raised when API returns an error response or unexpected data."""
    pass

class DataProcessingError(StockDataError):
    """Raised when data processing or validation fails."""
    pass

class FileOperationError(StockDataError):
    """Raised when file I/O operations fail."""
    pass
