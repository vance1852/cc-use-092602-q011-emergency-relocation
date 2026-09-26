"""应急转移服务向 API 和 CLI 暴露的稳定错误。"""


class EvacuationError(RuntimeError):
    code = "evacuation_error"
    status = 400


class NotFound(EvacuationError):
    code = "not_found"
    status = 404


class Conflict(EvacuationError):
    code = "conflict"
    status = 409


class Forbidden(EvacuationError):
    code = "forbidden"
    status = 403


class InvalidState(EvacuationError):
    code = "invalid_state"
    status = 409


class ValidationFailed(EvacuationError):
    code = "validation_failed"
    status = 422
