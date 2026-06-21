import sys

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):  # type: ignore[no-redef]
        pass


class SourceType(StrEnum):
    MY_BLOG = "my_blog"
    EXT_BLOG = "ext_blog"
    JOB_POSTING = "job_posting"
    BLOG_SNAPSHOT = "blog_snapshot"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ChatRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
