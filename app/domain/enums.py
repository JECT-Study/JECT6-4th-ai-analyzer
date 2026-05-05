from enum import StrEnum


class SourceType(StrEnum):
    MY_BLOG = "my_blog"
    EXT_BLOG = "ext_blog"
    JOB_POSTING = "job_posting"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
