from datetime import date

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import (
    ActivityLog,
    Course,
    CourseSession,
    CourseSessionProposal,
    DataIssue,
    EntityAliasProposal,
    Registration,
    SourceRecord,
    Student,
    StudentIdentityProposal,
    Team,
    TrainingRecord,
)
from .access import (
    can_view_inventory,
    instructor_for_user,
    is_operations_admin,
)
from .identity import format_emirates_id
from .dashboard_history import project_historical_dashboard


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
    today = timezone.localdate()

    def requested_date(name, default):
        value = request.GET.get(name, "").strip()
        if not value:
            return default
        try:
            return date.fromisoformat(value)
        except ValueError:
            return default

    date_from = requested_date("date_from", date(today.year, 1, 1))
    date_to = requested_date("date_to", date(today.year, 12, 31))
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    all_history = request.GET.get("all_history", "").strip() == "1"
    selected_course = request.GET.get("course", "").strip()
    selected_team = request.GET.get("team", "").strip()
    administrator = is_operations_admin(request.user)
    instructor = instructor_for_user(request.user)
    sessions = CourseSession.objects.select_related("course", "camp")
    if not administrator:
        if instructor is None:
            sessions = sessions.none()
        else:
            sessions = sessions.filter(
                instructor_assignments__instructor=instructor
            )
    if selected_course.isdigit():
        sessions = sessions.filter(course_id=int(selected_course))
    else:
        selected_course = ""
    if selected_team.isdigit():
        sessions = sessions.filter(
            instructor_assignments__instructor__team_id=int(selected_team)
        )
    else:
        selected_team = ""
    if not all_history:
        sessions = sessions.filter(
            start_date__gte=date_from,
            start_date__lte=date_to,
        )
    sessions = sessions.distinct()

    registrations = Registration.objects.filter(requested_session__in=sessions)
    selected_registrations = registrations.filter(
        selected_for_roster=True,
    ).exclude(
        status=Registration.Status.REJECTED,
    ).exclude(
        duplicate_review_status=(
            Registration.DuplicateReviewStatus.EXCLUDED
        )
    )
    training_records = TrainingRecord.objects.filter(
        session__in=sessions,
        duplicate_flag=False,
    ).exclude(
        record_status=TrainingRecord.RecordStatus.CANCELLED,
    )

    training_by_participation = {}
    for record in training_records.order_by("updated_at", "id").values(
        "id",
        "student_id",
        "session_id",
        "session__start_date",
        "result",
        "is_hp",
        "is_ttt",
        "raw_payload",
        "updated_at",
    ):
        training_by_participation[
            (record["student_id"], record["session_id"])
        ] = record
    training_rows = list(training_by_participation.values())
    represented_registration_ids = set()
    represented_student_sessions = set()
    for record in training_rows:
        payload = record["raw_payload"]
        if isinstance(payload, dict):
            registration_id = payload.get("registration_public_id")
            if registration_id:
                represented_registration_ids.add(str(registration_id))
        if record["student_id"]:
            represented_student_sessions.add(
                (record["student_id"], record["session_id"])
            )

    current_by_participation = {}
    for registration in selected_registrations.order_by(
        "updated_at", "id"
    ).values(
        "id",
        "public_id",
        "student_id",
        "requested_session_id",
        "requested_session__start_date",
        "assessment_status",
        "is_hp",
        "is_ttt",
        "updated_at",
    ):
        represented_by_id = (
            str(registration["public_id"]) in represented_registration_ids
        )
        represented_by_student = (
            registration["student_id"] is not None
            and (
                registration["student_id"],
                registration["requested_session_id"],
            )
            in represented_student_sessions
        )
        if represented_by_id or represented_by_student:
            continue
        if registration["student_id"] is None:
            key = ("registration", registration["id"])
        else:
            key = (
                "student_session",
                registration["student_id"],
                registration["requested_session_id"],
            )
        current_by_participation[key] = registration
    current_registration_rows = list(current_by_participation.values())

    rejected_filter = Q(status=Registration.Status.REJECTED) | Q(
        duplicate_review_status=Registration.DuplicateReviewStatus.EXCLUDED
    )
    rejected = registrations.filter(rejected_filter).distinct()

    totals = {
        "courses": sessions.count(),
        "enrolled": len(training_rows) + len(current_registration_rows),
        "passed": sum(
            record["result"] == TrainingRecord.Result.PASS
            for record in training_rows
        )
        + sum(
            registration["assessment_status"] == "passed"
            for registration in current_registration_rows
        ),
        "failed": sum(
            record["result"] == TrainingRecord.Result.FAIL
            for record in training_rows
        )
        + sum(
            registration["assessment_status"] == "failed"
            for registration in current_registration_rows
        ),
        "rejected": rejected.count(),
        "hp": sum(record["is_hp"] for record in training_rows)
        + sum(
            registration["is_hp"]
            for registration in current_registration_rows
        ),
        "ttt": sum(record["is_ttt"] for record in training_rows)
        + sum(
            registration["is_ttt"]
            for registration in current_registration_rows
        ),
    }

    historical_projection = project_historical_dashboard(
        administrator=administrator,
        instructor=instructor,
        date_from=date_from,
        date_to=date_to,
        all_history=all_history,
        selected_course=selected_course,
        selected_team=selected_team,
    )
    # Historical course proposals remain proposals until an administrator
    # approves them.  Their dated student evidence can be reported without
    # inflating the confirmed Course Sessions counter.
    for key in ("enrolled", "passed", "failed", "rejected", "hp", "ttt"):
        totals[key] += historical_projection.totals[key]

    for key, value in list(totals.items()):
        totals[f"{key}_digits"] = f"{value:04d}"

    month_values = {
        month: {"enrolled": 0, "passed": 0, "failed": 0, "rejected": 0}
        for month in range(1, 13)
    }
    for record in training_rows:
        session_date = record["session__start_date"]
        if session_date is None:
            continue
        month = month_values[session_date.month]
        month["enrolled"] += 1
        if record["result"] == TrainingRecord.Result.PASS:
            month["passed"] += 1
        elif record["result"] == TrainingRecord.Result.FAIL:
            month["failed"] += 1

    for registration in current_registration_rows:
        session_date = registration["requested_session__start_date"]
        if session_date is None:
            continue
        month = month_values[session_date.month]
        month["enrolled"] += 1
        if registration["assessment_status"] == "passed":
            month["passed"] += 1
        elif registration["assessment_status"] == "failed":
            month["failed"] += 1

    for registration in registrations.values(
        "requested_session__start_date",
        "status",
        "duplicate_review_status",
    ):
        session_date = registration["requested_session__start_date"]
        if session_date is None:
            continue
        month = month_values[session_date.month]
        if (
            registration["status"] == Registration.Status.REJECTED
            or registration["duplicate_review_status"]
            == Registration.DuplicateReviewStatus.EXCLUDED
        ):
            month["rejected"] += 1

    for month_number, historical_values in (
        historical_projection.month_values.items()
    ):
        for key in ("enrolled", "passed", "failed", "rejected"):
            month_values[month_number][key] += historical_values[key]

    chart_peak = max(
        [1]
        + [value["enrolled"] for value in month_values.values()]
        + [
            value["passed"] + value["failed"] + value["rejected"]
            for value in month_values.values()
        ]
    )
    chart_max = max(5, ((chart_peak + 4) // 5) * 5)
    chart_height = 190
    month_names = [
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    ]
    chart_rows = []
    line_points = []
    for index, month_name in enumerate(month_names, start=1):
        values = month_values[index]
        x = 35 + ((index - 1) * 55)
        passed_height = values["passed"] * chart_height / chart_max
        failed_height = values["failed"] * chart_height / chart_max
        rejected_height = values["rejected"] * chart_height / chart_max
        enrolled_y = chart_height - (
            values["enrolled"] * chart_height / chart_max
        )
        chart_rows.append(
            {
                "name": month_name,
                "x": x,
                "passed_y": chart_height - passed_height,
                "passed_height": passed_height,
                "failed_y": chart_height - passed_height - failed_height,
                "failed_height": failed_height,
                "rejected_y": (
                    chart_height
                    - passed_height
                    - failed_height
                    - rejected_height
                ),
                "rejected_height": rejected_height,
                "enrolled_y": enrolled_y,
                "enrolled": values["enrolled"],
                "passed": values["passed"],
                "failed": values["failed"],
                "rejected": values["rejected"],
            }
        )
        line_points.append(f"{x + 14},{enrolled_y:.2f}")
    chart_ticks = [
        {
            "value": round(chart_max * step / 4),
            "y": chart_height - (chart_height * step / 4),
        }
        for step in range(5)
    ]

    operations = [
        {
            "name": "My Courses",
            "items": ["Your assignments", "QR code", "Student names"],
            "url": reverse("my_courses"),
        },
        {
            "name": "Students",
            "items": ["Student records", "Training history"],
            "url": reverse("students"),
        },
    ]
    if administrator:
        operations.extend(
            [
                {
                    "name": "Directory",
                    "items": ["Course master list", "Course status"],
                    "url": reverse("courses_sessions"),
                },
                {
                    "name": "People",
                    "items": ["Contact info", "Positions", "Team assignments"],
                    "url": reverse("people"),
                },
            ]
        )
    if can_view_inventory(request.user):
        operations.append(
            {
                "name": "Inventory",
                "items": ["Equipment", "Consumables", "Current inventory"],
                "url": reverse("inventory"),
            }
        )
    if administrator:
        operations.extend(
            [
                {
                    "name": "Reports",
                    "items": ["Course information", "Saved files", "Uploaded files"],
                    "url": reverse("reports"),
                },
                {
                    "name": "Data Quality",
                    "items": ["Review duplicates", "Previous matches", "Errors"],
                    "url": reverse("data_quality"),
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
            "totals": totals,
            "courses": Course.objects.filter(active=True).order_by(
                "title_english"
            ),
            "teams": Team.objects.filter(active=True).order_by("name"),
            "selected_course": selected_course,
            "selected_team": selected_team,
            "all_history": all_history,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "chart_rows": chart_rows,
            "chart_ticks": reversed(chart_ticks),
            "chart_height": chart_height,
            "chart_line_points": " ".join(line_points),
            "operations": operations,
            "review_summary": review_summary,
            "historical_totals": historical_projection.totals,
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
