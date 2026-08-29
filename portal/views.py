from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    ActivityLog,
    CourseSession,
    CourseSessionProposal,
    DataIssue,
    EntityAliasProposal,
    Registration,
    SourceRecord,
    Student,
    StudentIdentityProposal,
    TrainingRecord,
)
from .access import can_manage_inventory, is_operations_admin
from .identity import format_emirates_id


MODULES = {
    "students": {
        "title": "Students",
        "description":
            "Search master profiles and course history.",
    },
    "training": {
        "title": "Training Records",
        "description":
            "Attendance, assessments, results and duplicate review.",
    },
    "courses": {
        "title": "Courses & Sessions",
        "description":
            "Course dates, instructors, camps, capacity and status.",
    },
    "registrations": {
        "title": "Registrations",
        "description":
            "Public registration links, submissions and approvals.",
    },
    "schedule": {
        "title": "Schedule",
        "description":
            "J35 planning and instructor assignments.",
    },
    "people": {
        "title": "People & Teams",
        "description":
            "Instructors, teams, hierarchy and responsibilities.",
    },
    "inventory": {
        "title": "Inventory",
        "description":
            "Items, issues, returns and consumption.",
    },
    "reports": {
        "title": "Reports & Exports",
        "description":
            "External Excel, stamped lists and print workflows.",
    },
    "data-quality": {
        "title": "Data Quality",
        "description":
            "Duplicate registrations, master-list matches and previous-course review.",
    },
}


@login_required
def dashboard(request):
    stats = [
        {
            "value": Student.objects.count(),
            "label": "Approved students",
            "tone": "orange",
        },
        {
            "value": TrainingRecord.objects.count(),
            "label": "Training records",
            "tone": "blue",
        },
        {
            "value": CourseSession.objects.count(),
            "label": "Approved sessions",
            "tone": "green",
        },
        {
            "value": Registration.objects.count(),
            "label": "Registrations",
            "tone": "purple",
        },
    ]

    operations = [
        {
            "name": "My Courses",
            "description": "Course information, QR registration, lists and results.",
            "url": reverse("my_courses"),
            "code": "MC",
        },
        {
            "name": "Students",
            "description": "Master student records and training history.",
            "url": reverse("students"),
            "code": "ST",
        },
    ]
    if can_manage_inventory(request.user):
        operations.append(
            {
                "name": "Inventory",
                "description": "Equipment, consumables and current counts.",
                "url": reverse("inventory"),
                "code": "IN",
            }
        )
    if is_operations_admin(request.user):
        operations.extend(
            [
                {
                    "name": "Course Directory",
                    "description": "Master list of every course session and its current status.",
                    "url": reverse("courses_sessions"),
                    "code": "CD",
                },
                {
                    "name": "People & Teams",
                    "description": "Instructors, positions and team assignments.",
                    "url": reverse("people"),
                    "code": "PT",
                },
                {
                    "name": "Reports",
                    "description": "Course completion, saved lists and uploads.",
                    "url": reverse("reports"),
                    "code": "RE",
                },
                {
                    "name": "Data Quality",
                    "description": "Review duplicate registrations and previous-course matches.",
                    "url": reverse("data_quality"),
                    "code": "DQ",
                },
            ]
        )

    review_summary = {
        "student_proposals":
            StudentIdentityProposal.objects.exclude(
                proposal_status=(
                    StudentIdentityProposal
                    .ProposalStatus
                    .APPROVED
                )
            ).count(),
        "course_proposals":
            CourseSessionProposal.objects.exclude(
                proposal_status=(
                    CourseSessionProposal
                    .ProposalStatus
                    .APPROVED
                )
            ).count(),
        "alias_reviews":
            EntityAliasProposal.objects.filter(
                proposal_status=(
                    EntityAliasProposal
                    .ProposalStatus
                    .REVIEW_REQUIRED
                )
            ).count(),
        "data_issues":
            DataIssue.objects.filter(
                status__in=[
                    DataIssue.Status.OPEN,
                    DataIssue.Status.REVIEWING,
                ]
            ).count(),
        "evidence_rows": SourceRecord.objects.count(),
    }

    return render(
        request,
        "portal/dashboard.html",
        {
            "stats": stats,
            "operations": operations,
            "review_summary": review_summary,
        },
    )


@login_required
def module_page(request, module_key):
    module = MODULES[module_key]

    data_quality_links = []

    if module_key == "data-quality":
        data_quality_links = [
            {
                "label": "Student identity proposals",
                "url": reverse(
                    "admin:portal_studentidentityproposal_changelist"
                ),
            },
            {
                "label": "Course-session proposals",
                "url": reverse(
                    "admin:portal_coursesessionproposal_changelist"
                ),
            },
            {
                "label": "Camp and instructor aliases",
                "url": reverse(
                    "admin:portal_entityaliasproposal_changelist"
                ),
            },
            {
                "label": "Open data issues",
                "url": reverse(
                    "admin:portal_dataissue_changelist"
                ),
            },
            {
                "label": "Historical evidence",
                "url": reverse(
                    "admin:portal_sourcerecord_changelist"
                ),
            },
            {
                "label": "Activity log",
                "url": reverse(
                    "admin:portal_activitylog_changelist"
                ),
            },
        ]

    return render(
        request,
        "portal/module_page.html",
        {
            "module": module,
            "module_key": module_key,
            "data_quality_links": data_quality_links,
        },
    )


@login_required
def student_list(request):
    students = Student.objects.annotate(
        training_count=Count(
            "training_records",
            distinct=True,
        )
    )

    query = request.GET.get("q", "").strip()
    identity_status = request.GET.get(
        "identity_status",
        "",
    )
    active_filter = request.GET.get("active", "yes")
    sort = request.GET.get("sort", "name")

    if query:
        students = students.filter(
            Q(name_english__icontains=query)
            | Q(name_arabic__icontains=query)
            | Q(eid__icontains=query)
            | Q(email__icontains=query)
            | Q(phone__icontains=query)
        )

    if identity_status:
        students = students.filter(
            identity_status=identity_status
        )

    if active_filter in ["yes", "no"]:
        students = students.filter(
            active=(active_filter == "yes")
        )

    sorting = {
        "name": ("name_english", "name_arabic", "eid"),
        "name_desc": (
            "-name_english",
            "-name_arabic",
            "-eid",
        ),
        "eid": ("eid", "name_english"),
        "newest": ("-created_at",),
    }

    students = students.order_by(
        *sorting.get(sort, sorting["name"])
    )

    try:
        page_size = int(
            request.GET.get("page_size", "50")
        )
    except ValueError:
        page_size = 50

    if page_size not in [25, 50, 100]:
        page_size = 50

    paginator = Paginator(students, page_size)
    page_obj = paginator.get_page(
        request.GET.get("page")
    )

    for student in page_obj.object_list:
        student.formatted_eid = format_emirates_id(
            student.eid
        )

    query_parameters = request.GET.copy()
    query_parameters.pop("page", None)

    return render(
        request,
        "portal/student_list.html",
        {
            "page_obj": page_obj,
            "total_matches": paginator.count,
            "query": query,
            "identity_status": identity_status,
            "active_filter": active_filter,
            "sort": sort,
            "page_size": page_size,
            "query_string":
                query_parameters.urlencode(),
            "identity_choices":
                Student.IdentityStatus.choices,
        },
    )


@login_required
def student_detail(request, public_id):
    student = get_object_or_404(
        Student,
        public_id=public_id,
    )
    student.formatted_eid = format_emirates_id(
        student.eid
    )

    training_records = (
        student.training_records
        .select_related(
            "session",
            "session__course",
            "session__camp",
        )
        .order_by(
            "-session__start_date",
            "session__course__title_english",
        )
    )

    return render(
        request,
        "portal/student_detail.html",
        {
            "student": student,
            "training_records": training_records,
        },
    )


@login_required
@require_POST
def student_print(request):
    mode = request.POST.get("mode", "selected")

    if mode == "shown":
        selected_ids = request.POST.getlist(
            "shown_ids"
        )
    else:
        selected_ids = request.POST.getlist(
            "student_ids"
        )

    students = list(
        Student.objects.filter(
            pk__in=selected_ids
        ).order_by(
            "name_english",
            "name_arabic",
            "eid",
        )
    )

    for student in students:
        student.formatted_eid = format_emirates_id(
            student.eid
        )

    ActivityLog.objects.create(
        actor=request.user,
        action=ActivityLog.Action.PRINT,
        object_type="Student",
        object_id=mode,
        description=(
            f"Prepared {len(students)} students "
            f"for browser printing"
        ),
        details={
            "mode": mode,
            "record_count": len(students),
        },
    )

    return render(
        request,
        "portal/student_print.html",
        {
            "students": students,
            "mode": mode,
            "prepared_at": timezone.localtime(),
            "prepared_by": request.user.get_full_name() or request.user.username,
        },
    )
