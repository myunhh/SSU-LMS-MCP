"""LMS 읽기 전용 어댑터."""

from .assignments import list_all_deadlines, list_assignments
from .courses import list_course_ids, list_courses
from .materials import list_all_materials, list_materials
from .notices import list_all_notices, list_discussions, list_notices

__all__ = [
    "list_all_deadlines",
    "list_all_materials",
    "list_all_notices",
    "list_assignments",
    "list_course_ids",
    "list_courses",
    "list_discussions",
    "list_materials",
    "list_notices",
]
