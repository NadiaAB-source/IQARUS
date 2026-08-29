from django.core.exceptions import PermissionDenied

from .models import CourseInstructor, Instructor


ADMINISTRATOR_ROLES = {
    "li",
    "lead instructor",
    "training director",
    "training manager",
    "project coordinator",
}


def instructor_for_user(user):
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        Instructor.objects.filter(user=user, active=True)
        .prefetch_related("roles")
        .first()
    )


def is_operations_admin(user):
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or user.is_staff:
        return True
    instructor = instructor_for_user(user)
    if instructor is None:
        return False
    role_names = {
        name.strip().casefold()
        for name in instructor.roles.filter(active=True).values_list(
            "name", flat=True
        )
    }
    return bool(role_names & ADMINISTRATOR_ROLES)


def can_create_course(user):
    return is_operations_admin(user)


def can_access_session(user, session):
    if is_operations_admin(user):
        return True
    instructor = instructor_for_user(user)
    if instructor is None:
        return False
    return CourseInstructor.objects.filter(
        session=session,
        instructor=instructor,
    ).exists()


def require_session_access(user, session):
    if not can_access_session(user, session):
        raise PermissionDenied("This course is not assigned to your account.")


def can_manage_inventory(user):
    if is_operations_admin(user):
        return True
    instructor = instructor_for_user(user)
    return bool(instructor and instructor.is_inventory_supervisor)


def can_view_inventory(user):
    return can_manage_inventory(user) or instructor_for_user(user) is not None
