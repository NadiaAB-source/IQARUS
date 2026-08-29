
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Q
from django.shortcuts import get_object_or_404, render

from .models import (
    Camp,
    Course,
    CourseSession,
    CourseSessionProposal,
)
from .course_services import display_reference
from .access import is_operations_admin


def _text_lookups(model, prefix="", depth=0, visited=None):
    visited = set(visited or set())

    if model in visited:
        return []

    visited.add(model)
    lookups = []

    for field in model._meta.get_fields():
        if (
            getattr(field, "concrete", False)
            and isinstance(field, (models.CharField, models.TextField))
        ):
            lookups.append(f"{prefix}{field.name}")

        elif (
            depth < 1
            and getattr(field, "concrete", False)
            and getattr(field, "many_to_one", False)
            and getattr(field, "related_model", None)
        ):
            lookups.extend(
                _text_lookups(
                    field.related_model,
                    prefix=f"{prefix}{field.name}__",
                    depth=depth + 1,
                    visited=visited.copy(),
                )
            )

    return lookups


def _session_row(session):
    instructors = list(session.instructors.all())
    instructor_names = ", ".join(str(item) for item in instructors)

    status_method = getattr(session, "get_status_display", None)
    status = status_method() if callable(status_method) else session.status

    return {
        "public_id": session.public_id,
        "course": session.course.title_english or str(session.course),
        "reference_code": session.reference_code or "Not assigned",
        "display_reference": display_reference(session),
        "start_date": session.start_date,
        "end_date": session.end_date,
        "camp": str(session.camp) if session.camp else "Not assigned",
        "status": status,
        "instructors": instructor_names or "Not assigned",
        "registration_published": session.registration_published,
    }


@login_required
def course_session_list(request):
    if not is_operations_admin(request.user):
        raise PermissionDenied("The master course directory is limited to administrator roles.")
    queryset = (
        CourseSession.objects
        .select_related("course", "camp")
        .prefetch_related("instructors")
    )

    search_text = request.GET.get("q", "").strip()
    course_id = request.GET.get("course", "").strip()
    camp_id = request.GET.get("camp", "").strip()
    status = request.GET.get("status", "").strip()
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()

    if search_text:
        search_query = Q()

        for lookup in _text_lookups(CourseSession):
            search_query |= Q(**{f"{lookup}__icontains": search_text})

        queryset = queryset.filter(search_query)

    if course_id:
        queryset = queryset.filter(course_id=course_id)

    if camp_id:
        queryset = queryset.filter(camp_id=camp_id)

    if status:
        queryset = queryset.filter(status=status)

    if date_from:
        queryset = queryset.filter(end_date__gte=date_from)

    if date_to:
        queryset = queryset.filter(start_date__lte=date_to)

    queryset = queryset.distinct().order_by("-start_date", "-id")

    paginator = Paginator(queryset, 25)
    page_obj = paginator.get_page(request.GET.get("page"))
    rows = [_session_row(session) for session in page_obj.object_list]

    status_field = CourseSession._meta.get_field("status")
    status_choices = [
        (str(value), str(label))
        for value, label in status_field.choices
    ]

    query_copy = request.GET.copy()
    query_copy.pop("page", None)

    context = {
        "active_module": "courses_sessions",
        "page_title": "Courses & Sessions",
        "header_title": "Courses & Sessions",
        "workspace_title": "Courses & Sessions",
        "rows": rows,
        "page_obj": page_obj,
        "total_courses": Course.objects.count(),
        "total_sessions": CourseSession.objects.count(),
        "filtered_count": queryset.count(),
        "review_count": CourseSessionProposal.objects.exclude(
            proposal_status="approved"
        ).count(),
        "published_count": CourseSession.objects.filter(
            registration_published=True
        ).count(),
        "courses": Course.objects.all().order_by("pk"),
        "camps": Camp.objects.all().order_by("pk"),
        "status_choices": status_choices,
        "query_without_page": query_copy.urlencode(),
        "current": {
            "q": search_text,
            "course": course_id,
            "camp": camp_id,
            "status": status,
            "date_from": date_from,
            "date_to": date_to,
        },
    }

    return render(
        request,
        "portal/course_session_list.html",
        context,
    )


@login_required
def course_session_detail(request, public_id):
    if not is_operations_admin(request.user):
        raise PermissionDenied("The master course directory is limited to administrator roles.")
    session = get_object_or_404(
        CourseSession.objects
        .select_related("course", "camp")
        .prefetch_related("instructors"),
        public_id=public_id,
    )
    session.display_reference = display_reference(session)

    proposals = CourseSessionProposal.objects.filter(
        approved_session=session
    ).order_by("created_at")

    status_method = getattr(session, "get_status_display", None)
    status = status_method() if callable(status_method) else session.status

    context = {
        "active_module": "courses_sessions",
        "page_title": "Courses & Sessions",
        "header_title": "Courses & Sessions",
        "workspace_title": "Courses & Sessions",
        "session": session,
        "status": status,
        "instructors": session.instructors.all(),
        "proposals": proposals,
    }

    return render(
        request,
        "portal/course_session_detail.html",
        context,
    )
