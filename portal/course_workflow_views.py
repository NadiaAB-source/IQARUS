from datetime import date
from pathlib import Path
import uuid
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Max, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST, require_http_methods

from .access import (
    can_access_session,
    can_create_course,
    can_manage_inventory,
    can_view_inventory,
    instructor_for_user,
    is_operations_admin,
    require_session_access,
)
from .course_services import (
    build_stamped_roster_workbook,
    day_one_list_filename,
    display_reference,
    safe_reference_code,
    send_course_information,
)
from .duplicate_services import build_registration_review_rows
from .forms import CourseCreationForm
from .identity import format_emirates_id
from .models import (
    ActivityLog,
    CourseInstructor,
    CourseInstructorInventoryUsage,
    CourseInventoryUsage,
    CourseRosterSnapshot,
    CourseRosterSnapshotItem,
    CourseSession,
    Instructor,
    InstructorInventoryBalance,
    InstructorInventoryMovement,
    InstructorRole,
    InventoryItem,
    Registration,
    StampedListArchive,
    Team,
    TrainingRecord,
)


ROSTER_ROOT = (Path(settings.BASE_DIR).parent / "saved course lists").resolve()


def _session(public_id):
    return get_object_or_404(
        CourseSession.objects.select_related("course", "camp", "created_by"),
        public_id=public_id,
    )


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (forwarded.split(",", 1)[0].strip() if forwarded else None) or (
        request.META.get("REMOTE_ADDR") or None
    )


def _course_log_details(session):
    return {
        "reference_code": session.reference_code,
        "course": session.course.title_english,
        "camp": session.camp.name if session.camp_id else "",
        "service_branch": session.service_branch_name,
        "start_date": session.start_date.isoformat() if session.start_date else None,
        "end_date": session.end_date.isoformat() if session.end_date else None,
        "status": session.status,
        "instructors": list(
            session.instructor_assignments.order_by(
                "instructor__name_english"
            ).values_list("instructor__name_english", flat=True)
        ),
    }


def _unique_reference_code(course, camp, start_date, exclude_session=None):
    reference = safe_reference_code(course, camp, start_date)
    candidate = reference
    sequence = 2
    existing = CourseSession.objects.all()
    if exclude_session is not None:
        existing = existing.exclude(pk=exclude_session.pk)
    while existing.filter(reference_code=candidate).exists():
        suffix = f"-{sequence}"
        candidate = reference[: 100 - len(suffix)] + suffix
        sequence += 1
    return candidate


def _course_form_initial(session):
    return {
        "course": session.course_id,
        "camp": session.camp_id,
        "start_date": session.start_date,
        "end_date": session.end_date,
        "capacity": session.capacity,
        "instructor_student_ratio": session.instructor_student_ratio,
        "service_branch": session.service_branch,
        "service_branch_other": session.service_branch_other,
        "instructors": list(
            session.instructor_assignments.values_list(
                "instructor_id", flat=True
            )
        ),
        "poc_name": session.poc_name,
        "poc_contact_number": session.poc_contact_number,
        "poc_location_url": session.poc_location_url,
    }


def _course_delete_blockers(session):
    checks = [
        ("student registrations", session.registrations.exists()),
        ("training records", session.training_records.exists()),
        ("saved Day 1 lists", session.roster_snapshots.exists()),
        ("stamped lists", session.stamped_list_archives.exists()),
        ("instructor inventory reports", session.instructor_inventory_usage.exists()),
        ("legacy inventory records", session.inventory_usage.exists()),
        ("historical source evidence", session.source_evidence.exists()),
    ]
    if session.external_upload_generated_at or session.external_upload_confirmed_at:
        checks.append(("external upload history", True))
    return [label for label, present in checks if present]


def _nonnegative_integer(request, field_name, item_name):
    try:
        value = int(request.POST.get(field_name, "0") or 0)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Enter whole-number quantities for {item_name}."
        ) from exc
    if value < 0:
        raise ValidationError(f"Quantities cannot be negative for {item_name}.")
    return value


def _submitted_duplicate_reviews(request):
    prefix = "duplicate_decision_"
    return [
        {
            "registration_public_id": key[len(prefix):],
            "decision": value,
        }
        for key, value in request.POST.items()
        if key.startswith(prefix) and value
    ]


def _registration_rows(session):
    registrations = list(
        Registration.objects.filter(requested_session=session)
        .select_related(
            "student",
            "requested_session",
            "requested_session__course",
            "duplicate_reviewed_by",
        )
        .order_by("submitted_at", "id")
    )
    return build_registration_review_rows(registrations)


@login_required
def my_courses(request):
    administrator = is_operations_admin(request.user)
    instructor = instructor_for_user(request.user)
    sessions = CourseSession.objects.select_related("course", "camp")
    if not administrator:
        if instructor is None:
            sessions = sessions.none()
        else:
            sessions = sessions.filter(instructor_assignments__instructor=instructor)
    search = request.GET.get("q", "").strip()
    if search:
        sessions = sessions.filter(
            Q(reference_code__icontains=search)
            | Q(course__title_english__icontains=search)
            | Q(camp__name__icontains=search)
        )
    sessions = sessions.distinct().order_by("-start_date", "-id")
    rows = []
    for session in sessions:
        assignments = list(
            session.instructor_assignments.select_related("instructor").order_by(
                "assignment_role", "instructor__name_english"
            )
        )
        own_assignment = next(
            (
                assignment
                for assignment in assignments
                if instructor and assignment.instructor_id == instructor.id
            ),
            None,
        )
        rows.append(
            {
                "session": session,
                "display_reference": display_reference(session),
                "assignments": assignments,
                "registration_count": session.registrations.count(),
                "selected_count": session.registrations.filter(
                    selected_for_roster=True
                ).count(),
                "own_assignment": own_assignment,
            }
        )
    return render(
        request,
        "portal/my_courses.html",
        {
            "rows": rows,
            "search": search,
            "administrator": administrator,
            "can_create": can_create_course(request.user),
            "instructor": instructor,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def create_course(request):
    if not can_create_course(request.user):
        raise PermissionDenied("Only an authorised Lead Instructor or administrator can create courses.")
    form = CourseCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        with transaction.atomic():
            candidate = _unique_reference_code(
                cleaned["course"],
                cleaned["camp"],
                cleaned["start_date"],
            )
            session = CourseSession.objects.create(
                course=cleaned["course"],
                camp=cleaned["camp"],
                start_date=cleaned["start_date"],
                end_date=cleaned["end_date"],
                capacity=cleaned["capacity"],
                instructor_student_ratio=cleaned["instructor_student_ratio"],
                service_branch=cleaned["service_branch"],
                service_branch_other=cleaned["service_branch_other"],
                poc_name=cleaned["poc_name"],
                poc_contact_number=cleaned["poc_contact_number"],
                poc_location_url=cleaned["poc_location_url"],
                reference_code=candidate,
                status=CourseSession.Status.REGISTRATION_CLOSED,
                registration_published=False,
                created_by=request.user,
                confirmed_at=timezone.now(),
            )
            assignments = []
            for instructor in cleaned["selected_instructors"]:
                assignments.append(
                    CourseInstructor.objects.create(
                        session=session,
                        instructor=instructor,
                        assignment_role=CourseInstructor.AssignmentRole.INSTRUCTOR,
                    )
                )
            ActivityLog.objects.create(
                actor=request.user,
                action=ActivityLog.Action.CREATE,
                object_type="CourseSession",
                object_id=str(session.public_id),
                description="Course created and instructor assignments confirmed.",
                details={"reference_code": candidate, "instructors": len(assignments)},
                ip_address=_client_ip(request),
            )
        assignments = list(
            CourseInstructor.objects.filter(session=session).select_related(
                "instructor", "instructor__user"
            )
        )
        failures = send_course_information(request, session, assignments)
        if failures:
            messages.warning(
                request,
                "Course created, but one or more emails could not be sent: "
                + "; ".join(failures),
            )
        else:
            messages.success(
                request,
                "Course created. Course information was emailed to every assigned instructor.",
            )
        return redirect("instructor_course_workspace", public_id=session.public_id)
    return render(
        request,
        "portal/course_create.html",
        {"form": form, "editing": False},
    )


@login_required
@require_http_methods(["GET", "POST"])
def edit_course(request, public_id):
    if not can_create_course(request.user):
        raise PermissionDenied(
            "Only an authorised course administrator can edit courses."
        )
    session = _session(public_id)
    if session.status in {
        CourseSession.Status.CANCELLED,
        CourseSession.Status.COMPLETED,
    }:
        messages.error(request, "Cancelled or completed courses cannot be edited.")
        return redirect(
            "instructor_course_workspace",
            public_id=session.public_id,
        )

    form = CourseCreationForm(
        request.POST or None,
        session=session,
        initial=_course_form_initial(session),
    )
    if request.method == "POST" and form.is_valid():
        cleaned = form.cleaned_data
        selected_instructors = cleaned["selected_instructors"]
        selected_ids = {instructor.id for instructor in selected_instructors}
        current_assignments = list(
            session.instructor_assignments.select_related("instructor")
        )
        current_ids = {
            assignment.instructor_id for assignment in current_assignments
        }
        added_ids = selected_ids - current_ids
        removed_ids = current_ids - selected_ids
        blocked_removals = list(
            CourseInstructorInventoryUsage.objects.filter(
                session=session,
                instructor_id__in=removed_ids,
            )
            .values_list("instructor__name_english", flat=True)
            .distinct()
        )
        if blocked_removals:
            form.add_error(
                "instructors",
                "Cannot remove instructors who already recorded course inventory use: "
                + ", ".join(blocked_removals),
            )
        else:
            field_values = {
                "course_id": cleaned["course"].id,
                "camp_id": cleaned["camp"].id,
                "start_date": cleaned["start_date"],
                "end_date": cleaned["end_date"],
                "capacity": cleaned["capacity"],
                "instructor_student_ratio": cleaned[
                    "instructor_student_ratio"
                ],
                "service_branch": cleaned["service_branch"],
                "service_branch_other": cleaned[
                    "service_branch_other"
                ],
                "poc_name": cleaned["poc_name"],
                "poc_contact_number": cleaned["poc_contact_number"],
                "poc_location_url": cleaned["poc_location_url"],
            }
            changed_fields = [
                field_name
                for field_name, value in field_values.items()
                if getattr(session, field_name) != value
            ]
            assignments_changed = bool(added_ids or removed_ids)
            if not changed_fields and not assignments_changed:
                messages.info(request, "No course changes were made.")
                return redirect(
                    "instructor_course_workspace",
                    public_id=session.public_id,
                )

            before = _course_log_details(session)
            with transaction.atomic():
                CourseSession.objects.select_for_update().get(pk=session.pk)
                for field_name, value in field_values.items():
                    setattr(session, field_name, value)
                session.reference_code = _unique_reference_code(
                    cleaned["course"],
                    cleaned["camp"],
                    cleaned["start_date"],
                    exclude_session=session,
                )
                session.course_information_sent_at = None
                session.save(
                    update_fields=list(field_values)
                    + [
                        "reference_code",
                        "course_information_sent_at",
                        "updated_at",
                    ]
                )
                if removed_ids:
                    session.instructor_assignments.filter(
                        instructor_id__in=removed_ids
                    ).delete()
                for instructor in selected_instructors:
                    CourseInstructor.objects.get_or_create(
                        session=session,
                        instructor=instructor,
                        defaults={
                            "assignment_role": (
                                CourseInstructor.AssignmentRole.INSTRUCTOR
                            )
                        },
                    )
                session.instructor_assignments.update(
                    acknowledged_at=None,
                    notified_at=None,
                )
                after = _course_log_details(session)
                ActivityLog.objects.create(
                    actor=request.user,
                    action=ActivityLog.Action.UPDATE,
                    object_type="CourseSession",
                    object_id=str(session.public_id),
                    description=(
                        "Course details or instructor assignments updated."
                    ),
                    details={
                        "before": before,
                        "after": after,
                        "changed_fields": changed_fields,
                        "added_instructor_ids": sorted(added_ids),
                        "removed_instructor_ids": sorted(removed_ids),
                        "acknowledgements_reset": True,
                    },
                    ip_address=_client_ip(request),
                )

            assignments = list(
                session.instructor_assignments.select_related(
                    "instructor", "instructor__user"
                )
            )
            failures = send_course_information(
                request,
                session,
                assignments,
            )
            if failures:
                messages.warning(
                    request,
                    "Course updated, but one or more emails could not be sent: "
                    + "; ".join(failures),
                )
            else:
                messages.success(
                    request,
                    "Course updated. Updated information was emailed only to the assigned instructors.",
                )
            return redirect(
                "instructor_course_workspace",
                public_id=session.public_id,
            )

    return render(
        request,
        "portal/course_create.html",
        {
            "form": form,
            "editing": True,
            "session": session,
        },
    )


@login_required
def course_workspace(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    session.display_reference = display_reference(session)
    instructor = instructor_for_user(request.user)
    assignments = list(
        session.instructor_assignments.select_related("instructor").order_by(
            "assignment_role", "instructor__name_english"
        )
    )
    own_assignment = next(
        (
            assignment
            for assignment in assignments
            if instructor and assignment.instructor_id == instructor.id
        ),
        None,
    )
    rows = _registration_rows(session)
    selected = [row for row in rows if row["registration"].selected_for_roster]
    course_usage = list(
        session.instructor_inventory_usage.select_related("instructor", "item")
    )
    usage_by_instructor = {}
    for usage in course_usage:
        usage_by_instructor.setdefault(usage.instructor_id, {})[
            usage.item_id
        ] = usage
    balances = list(
        InstructorInventoryBalance.objects.filter(
            instructor_id__in=[assignment.instructor_id for assignment in assignments]
        ).select_related("instructor", "item")
    )
    balances_by_instructor = {}
    for balance in balances:
        balances_by_instructor.setdefault(balance.instructor_id, {})[
            balance.item_id
        ] = balance
    inventory_sections = []
    inventory_manager = can_manage_inventory(request.user)
    for assignment in assignments:
        instructor_balances = balances_by_instructor.get(
            assignment.instructor_id, {}
        )
        instructor_usage = usage_by_instructor.get(assignment.instructor_id, {})
        item_ids = set(instructor_usage)
        item_ids.update(
            item_id
            for item_id, balance in instructor_balances.items()
            if balance.quantity_on_hand > 0
        )
        section_rows = []
        for item_id in sorted(
            item_ids,
            key=lambda value: (
                (
                    instructor_balances.get(value).item
                    if instructor_balances.get(value)
                    else instructor_usage[value].item
                ).category,
                (
                    instructor_balances.get(value).item
                    if instructor_balances.get(value)
                    else instructor_usage[value].item
                ).name,
            ),
        ):
            balance = instructor_balances.get(item_id)
            usage = instructor_usage.get(item_id)
            section_rows.append(
                {
                    "item": balance.item if balance else usage.item,
                    "balance": balance,
                    "usage": usage,
                }
            )
        inventory_sections.append(
            {
                "assignment": assignment,
                "rows": section_rows,
                "can_edit": inventory_manager
                or bool(instructor and instructor.id == assignment.instructor_id),
            }
        )
    delete_blockers = _course_delete_blockers(session)
    return render(
        request,
        "portal/course_workspace.html",
        {
            "session": session,
            "assignments": assignments,
            "own_assignment": own_assignment,
            "rows": rows,
            "registration_count": len(rows),
            "selected_count": len(selected),
            "passed_count": sum(
                row["registration"].assessment_status == "passed" for row in selected
            ),
            "warning_count": sum(
                row["review_state"] == "required" for row in rows
            ),
            "latest_roster": session.roster_snapshots.first(),
            "current_stamped_list": session.stamped_list_archives.filter(
                active=True
            ).first(),
            "inventory_sections": inventory_sections,
            "administrator": is_operations_admin(request.user),
            "can_manage_course": can_create_course(request.user),
            "can_edit_course": can_create_course(request.user)
            and session.status
            not in {
                CourseSession.Status.CANCELLED,
                CourseSession.Status.COMPLETED,
            },
            "delete_blockers": delete_blockers,
            "can_delete_course": not delete_blockers,
            "course_read_only": session.status == CourseSession.Status.CANCELLED,
        },
    )


@login_required
@require_POST
def cancel_course(request, public_id):
    session = _session(public_id)
    if not can_create_course(request.user):
        raise PermissionDenied("Only an authorised course administrator can cancel courses.")
    if session.status == CourseSession.Status.COMPLETED:
        messages.error(request, "A completed course cannot be cancelled.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    if session.status == CourseSession.Status.CANCELLED:
        messages.info(request, "This course is already cancelled.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    previous_status = session.status
    reason = request.POST.get("reason", "").strip()[:1000]
    details = _course_log_details(session)
    details.update({"previous_status": previous_status, "reason": reason})
    with transaction.atomic():
        session.status = CourseSession.Status.CANCELLED
        session.registration_published = False
        session.save(
            update_fields=["status", "registration_published", "updated_at"]
        )
        ActivityLog.objects.create(
            actor=request.user,
            action=ActivityLog.Action.CANCEL,
            object_type="CourseSession",
            object_id=str(session.public_id),
            description=f"Course cancelled: {session.reference_code}",
            details=details,
            ip_address=_client_ip(request),
        )
    messages.success(request, "Course cancelled. All course records were preserved.")
    return redirect("instructor_course_workspace", public_id=session.public_id)


@login_required
@require_POST
def delete_course(request, public_id):
    session = _session(public_id)
    if not can_create_course(request.user):
        raise PermissionDenied("Only an authorised course administrator can delete courses.")
    blockers = _course_delete_blockers(session)
    if blockers:
        messages.error(
            request,
            "This course contains "
            + ", ".join(blockers)
            + ". Cancel it instead so its records remain protected.",
        )
        return redirect("instructor_course_workspace", public_id=session.public_id)
    if request.POST.get("confirm", "") != "delete":
        messages.error(request, "Course deletion was not confirmed.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    public_id_text = str(session.public_id)
    details = _course_log_details(session)
    reason = request.POST.get("reason", "").strip()[:1000]
    details["reason"] = reason
    reference_code = session.reference_code
    with transaction.atomic():
        ActivityLog.objects.create(
            actor=request.user,
            action=ActivityLog.Action.DELETE,
            object_type="CourseSession",
            object_id=public_id_text,
            description=f"Empty course deleted: {reference_code}",
            details=details,
            ip_address=_client_ip(request),
        )
        session.delete()
    messages.success(request, f"Empty course {reference_code} was deleted and logged.")
    return redirect("my_courses")


@login_required
@require_POST
def acknowledge_course(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    if session.status == CourseSession.Status.CANCELLED:
        messages.error(request, "A cancelled course is read-only.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    instructor = instructor_for_user(request.user)
    if instructor is None:
        raise PermissionDenied("This administrator account has no instructor acknowledgement.")
    assignment = get_object_or_404(
        CourseInstructor, session=session, instructor=instructor
    )
    if assignment.acknowledged_at is None:
        assignment.acknowledged_at = timezone.now()
        assignment.save(update_fields=["acknowledged_at", "updated_at"])
        ActivityLog.objects.create(
            actor=request.user,
            action=ActivityLog.Action.UPDATE,
            object_type="CourseInstructor",
            object_id=str(assignment.id),
            description="Instructor acknowledged the course information.",
            details={"session_public_id": str(session.public_id)},
            ip_address=_client_ip(request),
        )
    messages.success(request, "Course information acknowledged.")
    return redirect("instructor_course_workspace", public_id=session.public_id)


@login_required
@require_POST
def confirm_external_upload(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    if session.status == CourseSession.Status.CANCELLED:
        messages.error(request, "A cancelled course is read-only.")
    elif session.external_upload_generated_at is None:
        messages.error(request, "Download the External Upload Excel before marking it complete.")
    else:
        session.external_upload_confirmed_at = timezone.now()
        session.external_upload_confirmed_by = request.user
        session.registration_published = False
        session.status = CourseSession.Status.COMPLETED
        session.save(
            update_fields=[
                "external_upload_confirmed_at",
                "external_upload_confirmed_by",
                "registration_published",
                "status",
                "updated_at",
            ]
        )
        ActivityLog.objects.create(
            actor=request.user,
            action=ActivityLog.Action.UPDATE,
            object_type="CourseSession",
            object_id=str(session.public_id),
            description="External upload marked complete.",
            details={},
            ip_address=_client_ip(request),
        )
        messages.success(request, "External upload marked complete.")
    return redirect("instructor_course_workspace", public_id=session.public_id)


@login_required
@require_POST
def resend_course_information(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    if session.status == CourseSession.Status.CANCELLED:
        messages.error(request, "A cancelled course is read-only.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    assignments = list(
        CourseInstructor.objects.filter(session=session).select_related(
            "instructor", "instructor__user"
        )
    )
    missing = [
        assignment.instructor.name_english
        for assignment in assignments
        if not (
            assignment.instructor.email
            or (
                assignment.instructor.user.email
                if assignment.instructor.user_id
                else ""
            )
        ).strip()
    ]
    if missing:
        messages.error(
            request,
            "Add email addresses before sending: " + ", ".join(missing),
        )
    else:
        failures = send_course_information(request, session, assignments)
        if failures:
            messages.error(request, "Email could not be sent: " + "; ".join(failures))
        else:
            messages.success(request, "Course information emailed to every instructor.")
    return redirect("instructor_course_workspace", public_id=session.public_id)


def _save_course_roster(request, session):
    from .student_services import find_or_create_student

    if session.status == CourseSession.Status.CANCELLED:
        raise ValidationError("A cancelled course is read-only.")

    registrations = list(
        Registration.objects.select_for_update()
        .filter(requested_session=session)
        .select_related(
            "student",
            "requested_session",
            "requested_session__course",
            "duplicate_reviewed_by",
        )
        .order_by("submitted_at", "id")
    )
    review_rows = {
        row["registration"].id: row
        for row in build_registration_review_rows(registrations)
    }
    allowed_results = {"pending", "passed", "failed"}
    allowed_decisions = {
        Registration.DuplicateReviewStatus.ALLOWED,
        Registration.DuplicateReviewStatus.EXCLUDED,
    }
    selected_count = 0
    for registration in registrations:
        key = str(registration.public_id)
        review_row = review_rows[registration.id]
        decision = request.POST.get(f"duplicate_decision_{key}", "").strip()
        if decision and decision not in allowed_decisions:
            raise ValidationError("An invalid duplicate review decision was submitted.")
        if not review_row["review_required"]:
            registration.duplicate_review_status = (
                Registration.DuplicateReviewStatus.CLEAR
            )
            registration.duplicate_review_notes = ""
            registration.duplicate_review_fingerprint = ""
            registration.duplicate_reviewed_by = None
            registration.duplicate_reviewed_at = None
        elif decision:
            duplicate_notes = request.POST.get(
                f"duplicate_notes_{key}", ""
            ).strip()[:2000]
            if (
                registration.duplicate_review_status != decision
                or registration.duplicate_review_notes != duplicate_notes
                or registration.duplicate_review_fingerprint
                != review_row["review_fingerprint"]
            ):
                registration.duplicate_review_status = decision
                registration.duplicate_review_notes = duplicate_notes
                registration.duplicate_review_fingerprint = review_row[
                    "review_fingerprint"
                ]
                registration.duplicate_reviewed_by = request.user
                registration.duplicate_reviewed_at = timezone.now()
        elif review_row["review_state"] == "required":
            registration.duplicate_review_status = (
                Registration.DuplicateReviewStatus.REQUIRED
            )
            registration.duplicate_review_fingerprint = ""
            registration.duplicate_reviewed_by = None
            registration.duplicate_reviewed_at = None

        selected = request.POST.get(f"selected_{key}") == "yes"
        if (
            review_row["review_required"]
            and registration.duplicate_review_status
            == Registration.DuplicateReviewStatus.EXCLUDED
        ):
            selected = False
        elif (
            review_row["review_required"]
            and registration.duplicate_review_status
            != Registration.DuplicateReviewStatus.ALLOWED
        ):
            if selected:
                raise ValidationError(
                    f"Review {registration} before selecting this student. "
                    "Choose Reviewed — allow or Reviewed — exclude, then save."
                )
            selected = False
        result = request.POST.get(f"result_{key}", "pending").strip()
        if result not in allowed_results:
            raise ValidationError("An invalid Pass/Fail value was submitted.")
        if not selected:
            result = "pending"
        if selected and registration.student_id is None:
            student, _ = find_or_create_student(registration)
            registration.student = student
        registration.selected_for_roster = selected
        if selected:
            registration.status = Registration.Status.APPROVED
        elif (
            review_row["review_required"]
            and registration.duplicate_review_status
            == Registration.DuplicateReviewStatus.EXCLUDED
        ):
            registration.status = Registration.Status.REJECTED
        elif review_row["review_required"] and (
            registration.duplicate_review_status
            == Registration.DuplicateReviewStatus.REQUIRED
        ):
            registration.status = Registration.Status.NEEDS_REVIEW
        else:
            registration.status = Registration.Status.PENDING
        registration.is_hp = selected and request.POST.get(f"hp_{key}") == "yes"
        registration.is_ttt = selected and request.POST.get(f"ttt_{key}") == "yes"
        registration.assessment_status = result
        registration.instructor_remarks = request.POST.get(
            f"remarks_{key}", ""
        ).strip()[:2000]
        if result in {"passed", "failed"}:
            registration.day1_attended = True
            registration.day2_attended = True
        else:
            registration.day1_attended = False
            registration.day2_attended = False
        registration.save(
            update_fields=[
                "student",
                "selected_for_roster",
                "status",
                "duplicate_review_status",
                "duplicate_review_notes",
                "duplicate_review_fingerprint",
                "duplicate_reviewed_by",
                "duplicate_reviewed_at",
                "is_hp",
                "is_ttt",
                "assessment_status",
                "instructor_remarks",
                "day1_attended",
                "day2_attended",
                "updated_at",
            ]
        )
        if selected:
            selected_count += 1
        else:
            TrainingRecord.objects.filter(
                session=session,
                raw_payload__registration_public_id=str(registration.public_id),
            ).delete()
        if selected and registration.student_id and (
            result != "pending" or registration.is_hp or registration.is_ttt
        ):
            record = TrainingRecord.objects.filter(
                session=session,
                raw_payload__registration_public_id=str(registration.public_id),
            ).first()
            values = {
                "student": registration.student,
                "session": session,
                "attendance": (
                    TrainingRecord.Attendance.PRESENT
                    if result in {"passed", "failed"}
                    else TrainingRecord.Attendance.PENDING
                ),
                "result": (
                    TrainingRecord.Result.PASS
                    if result == "passed"
                    else TrainingRecord.Result.FAIL
                    if result == "failed"
                    else TrainingRecord.Result.PENDING
                ),
                "record_status": (
                    TrainingRecord.RecordStatus.COMPLETED
                    if result in {"passed", "failed"}
                    else TrainingRecord.RecordStatus.REGISTERED
                ),
                "is_hp": registration.is_hp,
                "is_ttt": registration.is_ttt,
                "source_sheet": "IQARUS course workflow",
                "raw_payload": {
                    "registration_public_id": str(registration.public_id)
                },
                "notes": registration.instructor_remarks,
            }
            if record is None:
                TrainingRecord.objects.create(**values)
            else:
                for name, value in values.items():
                    setattr(record, name, value)
                record.save()
        elif selected:
            TrainingRecord.objects.filter(
                session=session,
                raw_payload__registration_public_id=str(registration.public_id),
            ).delete()
    return selected_count


@login_required
@require_POST
def save_course_roster(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    try:
        with transaction.atomic():
            count = _save_course_roster(request, session)
            ActivityLog.objects.create(
                actor=request.user,
                action=ActivityLog.Action.UPDATE,
                object_type="CourseSession",
                object_id=str(session.public_id),
                description="Course roster, results and HP/TTT selections saved.",
                details={
                    "selected_students": count,
                    "duplicate_reviews": _submitted_duplicate_reviews(request),
                },
                ip_address=_client_ip(request),
            )
    except ValidationError as exc:
        for error in exc.messages:
            messages.error(request, error)
    else:
        messages.success(request, f"Course roster saved for {count} selected students.")
    return redirect("instructor_course_workspace", public_id=session.public_id)


@login_required
@require_POST
def save_and_download_roster(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    if session.status == CourseSession.Status.CANCELLED:
        messages.error(request, "A cancelled course is read-only.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    final_path = None
    try:
        with transaction.atomic():
            count = _save_course_roster(request, session)
            registrations = list(
                Registration.objects.filter(
                    requested_session=session,
                    selected_for_roster=True,
                    status="approved",
                )
                .select_related("student")
                .order_by("submitted_at", "id")
            )
            if not registrations:
                raise ValidationError("Select at least one student before saving the list.")
            data, digest = build_stamped_roster_workbook(session, registrations)
            CourseSession.objects.select_for_update().get(pk=session.pk)
            latest_version = (
                CourseRosterSnapshot.objects.filter(session=session).aggregate(
                    maximum=Max("version")
                )["maximum"]
                or 0
            )
            version = latest_version + 1
            relative_path = Path(str(session.public_id), f"roster-v{version}-{uuid.uuid4().hex}.xlsx")
            final_path = (ROSTER_ROOT / relative_path).resolve()
            if ROSTER_ROOT not in final_path.parents:
                raise RuntimeError("Invalid saved-roster path.")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.write_bytes(data)
            snapshot = CourseRosterSnapshot.objects.create(
                session=session,
                version=version,
                stored_path=relative_path.as_posix(),
                sha256=digest,
                student_count=count,
                created_by=request.user,
            )
            CourseRosterSnapshotItem.objects.bulk_create(
                [
                    CourseRosterSnapshotItem(
                        snapshot=snapshot,
                        registration=registration,
                        serial_number=index,
                        email=registration.email_raw,
                        eid=format_emirates_id(
                            registration.eid_normalized or registration.eid_raw,
                            fallback="",
                        ),
                        name_english=registration.submitted_name_english,
                        name_arabic=registration.submitted_name_arabic,
                    )
                    for index, registration in enumerate(registrations, start=1)
                ]
            )
            ActivityLog.objects.create(
                actor=request.user,
                action=ActivityLog.Action.PRINT,
                object_type="CourseRosterSnapshot",
                object_id=str(snapshot.public_id),
                description="Reviewed Day 1 list saved and downloaded.",
                details={
                    "version": version,
                    "student_count": count,
                    "sha256": digest,
                    "duplicate_reviews": _submitted_duplicate_reviews(request),
                },
                ip_address=_client_ip(request),
            )
    except Exception:
        if final_path is not None:
            final_path.unlink(missing_ok=True)
        raise
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    filename = day_one_list_filename(session)
    response["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(filename)
    return response


@login_required
def download_saved_roster(request, snapshot_id):
    snapshot = get_object_or_404(
        CourseRosterSnapshot.objects.select_related("session"), public_id=snapshot_id
    )
    require_session_access(request.user, snapshot.session)
    file_path = (ROSTER_ROOT / snapshot.stored_path).resolve()
    if ROSTER_ROOT not in file_path.parents or not file_path.is_file():
        raise Http404("The saved list is unavailable.")
    response = FileResponse(
        open(file_path, "rb"),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''"
        + quote(day_one_list_filename(snapshot.session))
    )
    return response


@login_required
@require_POST
def save_course_inventory(request, public_id):
    session = _session(public_id)
    require_session_access(request.user, session)
    if session.status == CourseSession.Status.CANCELLED:
        messages.error(request, "A cancelled course is read-only.")
        return redirect("instructor_course_workspace", public_id=session.public_id)
    target_assignment = get_object_or_404(
        CourseInstructor.objects.select_related("instructor"),
        session=session,
        instructor_id=request.POST.get("instructor_id"),
    )
    current_instructor = instructor_for_user(request.user)
    if not (
        current_instructor
        and current_instructor.id == target_assignment.instructor_id
    ) and not can_manage_inventory(request.user):
        raise PermissionDenied("Each instructor records only their own course use.")
    try:
        with transaction.atomic():
            balances = {
                balance.item_id: balance
                for balance in InstructorInventoryBalance.objects.select_for_update()
                .filter(instructor=target_assignment.instructor)
                .select_related("item")
            }
            usages = {
                usage.item_id: usage
                for usage in CourseInstructorInventoryUsage.objects.select_for_update()
                .filter(
                    session=session,
                    instructor=target_assignment.instructor,
                )
                .select_related("item")
            }
            item_ids = set(balances) | set(usages)
            changes = []
            for item_id in item_ids:
                balance = balances.get(item_id)
                existing = usages.get(item_id)
                item = balance.item if balance else existing.item
                used = _nonnegative_integer(
                    request, f"used_{item_id}", item.name
                )
                consumed = _nonnegative_integer(
                    request, f"consumed_{item_id}", item.name
                )
                deteriorated = _nonnegative_integer(
                    request, f"deteriorated_{item_id}", item.name
                )
                notes = request.POST.get(
                    f"inventory_notes_{item_id}", ""
                ).strip()[:1000]
                old_consumed = existing.quantity_consumed if existing else 0
                old_deteriorated = (
                    existing.quantity_deteriorated if existing else 0
                )
                old_loss = old_consumed + old_deteriorated
                new_loss = consumed + deteriorated
                if balance is None:
                    if used or new_loss or notes:
                        raise ValidationError(
                            f"{item.name} has not been issued to "
                            f"{target_assignment.instructor.name_english}."
                        )
                    continue
                available_for_report = balance.quantity_on_hand + old_loss
                if used > available_for_report:
                    raise ValidationError(
                        f"{item.name}: used quantity exceeds the instructor's "
                        f"available balance of {available_for_report}."
                    )
                if new_loss > used:
                    raise ValidationError(
                        f"{item.name}: consumed plus deteriorated cannot exceed used."
                    )
                new_balance = available_for_report - new_loss
                quantity_change = new_balance - balance.quantity_on_hand
                if used or consumed or deteriorated or notes:
                    usage, _ = CourseInstructorInventoryUsage.objects.update_or_create(
                        session=session,
                        instructor=target_assignment.instructor,
                        item=item,
                        defaults={
                            "quantity_used": used,
                            "quantity_consumed": consumed,
                            "quantity_deteriorated": deteriorated,
                            "recorded_by": request.user,
                            "notes": notes,
                        },
                    )
                elif existing:
                    existing.delete()
                if quantity_change:
                    balance.quantity_on_hand = new_balance
                    balance.updated_by = request.user
                    balance.save(
                        update_fields=[
                            "quantity_on_hand",
                            "updated_by",
                            "updated_at",
                        ]
                    )
                    InstructorInventoryMovement.objects.create(
                        instructor=target_assignment.instructor,
                        item=item,
                        session=session,
                        movement_type=(
                            InstructorInventoryMovement.MovementType.COURSE_REPORT
                            if quantity_change < 0
                            else InstructorInventoryMovement.MovementType.CORRECTION
                        ),
                        quantity_change=quantity_change,
                        balance_after=new_balance,
                        recorded_by=request.user,
                        notes=(
                            f"Course report: used {used}, consumed {consumed}, "
                            f"deteriorated {deteriorated}."
                        ),
                    )
                changes.append(
                    {
                        "item": item.name,
                        "used": used,
                        "consumed": consumed,
                        "deteriorated": deteriorated,
                        "balance_after": new_balance,
                    }
                )
            ActivityLog.objects.create(
                actor=request.user,
                action=ActivityLog.Action.UPDATE,
                object_type="CourseInstructorInventoryUsage",
                object_id=(
                    f"{session.public_id}:{target_assignment.instructor.public_id}"
                ),
                description=(
                    "Instructor course equipment and consumable use saved."
                ),
                details={
                    "session_public_id": str(session.public_id),
                    "instructor": target_assignment.instructor.name_english,
                    "items": changes,
                },
                ip_address=_client_ip(request),
            )
    except ValidationError as exc:
        for error in exc.messages:
            messages.error(request, error)
    else:
        messages.success(
            request,
            f"{target_assignment.instructor.name_english}'s course inventory report was saved.",
        )
    return redirect("instructor_course_workspace", public_id=session.public_id)


@login_required
@require_http_methods(["GET", "POST"])
def inventory(request):
    if not can_view_inventory(request.user):
        raise PermissionDenied("Inventory access is limited to IQARUS instructors and administrators.")
    inventory_manager = can_manage_inventory(request.user)
    current_instructor = instructor_for_user(request.user)
    if request.method == "POST":
        if not inventory_manager:
            raise PermissionDenied("Only inventory supervisors and administrators can issue or refill stock.")
        action = request.POST.get("action")
        try:
            with transaction.atomic():
                if action == "add":
                    name = request.POST.get("name", "").strip()
                    category = request.POST.get("category", "")
                    quantity = _nonnegative_integer(
                        request, "quantity_on_hand", name or "the new item"
                    )
                    if not name:
                        raise ValidationError("Enter an item name.")
                    if InventoryItem.objects.filter(name__iexact=name).exists():
                        raise ValidationError("An inventory item with this name already exists.")
                    if category not in InventoryItem.Category.values:
                        raise ValidationError("Choose Equipment or Consumable.")
                    item = InventoryItem.objects.create(
                        name=name,
                        category=category,
                        unit=request.POST.get("unit", "item").strip() or "item",
                        quantity_on_hand=quantity,
                    )
                    ActivityLog.objects.create(
                        actor=request.user,
                        action=ActivityLog.Action.CREATE,
                        object_type="InventoryItem",
                        object_id=str(item.id),
                        description=f"Inventory item created: {item.name}",
                        details={"warehouse_quantity": quantity},
                        ip_address=_client_ip(request),
                    )
                elif action == "count":
                    item = get_object_or_404(
                        InventoryItem.objects.select_for_update(),
                        pk=request.POST.get("item_id"),
                    )
                    quantity = _nonnegative_integer(
                        request, "quantity_on_hand", item.name
                    )
                    previous = item.quantity_on_hand
                    item.quantity_on_hand = quantity
                    item.save(update_fields=["quantity_on_hand", "updated_at"])
                    ActivityLog.objects.create(
                        actor=request.user,
                        action=ActivityLog.Action.UPDATE,
                        object_type="InventoryItem",
                        object_id=str(item.id),
                        description=f"Warehouse count updated: {item.name}",
                        details={"before": previous, "after": quantity},
                        ip_address=_client_ip(request),
                    )
                elif action == "allocate":
                    item = get_object_or_404(
                        InventoryItem.objects.select_for_update(),
                        pk=request.POST.get("item_id"),
                        active=True,
                    )
                    instructor = get_object_or_404(
                        Instructor,
                        pk=request.POST.get("instructor_id"),
                        active=True,
                    )
                    quantity = _nonnegative_integer(
                        request, "quantity", item.name
                    )
                    if quantity < 1:
                        raise ValidationError("Issue or refill at least one item.")
                    if quantity > item.quantity_on_hand:
                        raise ValidationError(
                            f"Only {item.quantity_on_hand} {item.unit}(s) of "
                            f"{item.name} remain in warehouse stock."
                        )
                    movement_type = request.POST.get("movement_type", "issue")
                    if movement_type not in {
                        InstructorInventoryMovement.MovementType.ISSUE,
                        InstructorInventoryMovement.MovementType.REFILL,
                    }:
                        raise ValidationError("Choose Initial issue or Refill.")
                    balance, _ = InstructorInventoryBalance.objects.select_for_update().get_or_create(
                        instructor=instructor,
                        item=item,
                        defaults={"updated_by": request.user},
                    )
                    item.quantity_on_hand -= quantity
                    item.save(update_fields=["quantity_on_hand", "updated_at"])
                    balance.quantity_on_hand += quantity
                    balance.updated_by = request.user
                    balance.save(
                        update_fields=[
                            "quantity_on_hand",
                            "updated_by",
                            "updated_at",
                        ]
                    )
                    note = request.POST.get("notes", "").strip()[:1000]
                    movement = InstructorInventoryMovement.objects.create(
                        instructor=instructor,
                        item=item,
                        movement_type=movement_type,
                        quantity_change=quantity,
                        balance_after=balance.quantity_on_hand,
                        recorded_by=request.user,
                        notes=note,
                    )
                    ActivityLog.objects.create(
                        actor=request.user,
                        action=(
                            ActivityLog.Action.ISSUE
                            if movement_type
                            == InstructorInventoryMovement.MovementType.ISSUE
                            else ActivityLog.Action.REFILL
                        ),
                        object_type="InstructorInventoryMovement",
                        object_id=str(movement.id),
                        description=(
                            f"{movement.get_movement_type_display()}: "
                            f"{item.name} to {instructor.name_english}"
                        ),
                        details={
                            "quantity": quantity,
                            "instructor_balance": balance.quantity_on_hand,
                            "warehouse_balance": item.quantity_on_hand,
                        },
                        ip_address=_client_ip(request),
                    )
                else:
                    raise ValidationError("Choose a valid inventory action.")
        except (ValueError, ValidationError) as exc:
            errors = exc.messages if isinstance(exc, ValidationError) else ["Enter a whole-number quantity."]
            for error in errors:
                messages.error(request, error)
        else:
            messages.success(request, "Inventory updated.")
        return redirect("inventory")
    balances = InstructorInventoryBalance.objects.select_related(
        "instructor", "instructor__team", "item"
    ).filter(quantity_on_hand__gt=0)
    movements = InstructorInventoryMovement.objects.select_related(
        "instructor", "instructor__team", "item", "session", "recorded_by"
    )
    if not inventory_manager:
        balances = balances.filter(instructor=current_instructor)
        movements = movements.filter(instructor=current_instructor)

    balance_count = balances.count()
    history_count = movements.count()
    inventory_view = request.GET.get("view", "balances")
    if inventory_view not in {"balances", "history"}:
        inventory_view = "balances"

    filter_q = request.GET.get("q", "").strip()[:200]
    filter_team = request.GET.get("team", "").strip()
    if not filter_team.isdigit():
        filter_team = ""
    filter_movement = request.GET.get("movement_type", "").strip()
    if filter_movement not in InstructorInventoryMovement.MovementType.values:
        filter_movement = ""
    filter_date_from = request.GET.get("date_from", "").strip()
    filter_date_to = request.GET.get("date_to", "").strip()

    if filter_q:
        inventory_match = (
            Q(instructor__name_english__icontains=filter_q)
            | Q(instructor__email__icontains=filter_q)
            | Q(item__name__icontains=filter_q)
        )
        balances = balances.filter(inventory_match)
        movements = movements.filter(inventory_match)
    if inventory_manager and filter_team:
        balances = balances.filter(instructor__team_id=filter_team)
        movements = movements.filter(instructor__team_id=filter_team)
    if filter_movement:
        movements = movements.filter(movement_type=filter_movement)

    try:
        parsed_date_from = date.fromisoformat(filter_date_from)
    except ValueError:
        parsed_date_from = None
        filter_date_from = ""
    try:
        parsed_date_to = date.fromisoformat(filter_date_to)
    except ValueError:
        parsed_date_to = None
        filter_date_to = ""
    if parsed_date_from:
        movements = movements.filter(created_at__date__gte=parsed_date_from)
    if parsed_date_to:
        movements = movements.filter(created_at__date__lte=parsed_date_to)

    balance_page = Paginator(
        balances.order_by("instructor__name_english", "item__name"),
        25,
    ).get_page(request.GET.get("page"))
    movement_page = Paginator(
        movements.order_by("-created_at", "-id"),
        25,
    ).get_page(request.GET.get("page"))
    return render(
        request,
        "portal/inventory.html",
        {
            "items": InventoryItem.objects.all(),
            "instructors": Instructor.objects.filter(active=True).order_by(
                "name_english"
            ),
            "balance_page": balance_page,
            "movement_page": movement_page,
            "balance_count": balance_count,
            "history_count": history_count,
            "inventory_view": inventory_view,
            "filter_q": filter_q,
            "filter_team": filter_team,
            "filter_movement": filter_movement,
            "filter_date_from": filter_date_from,
            "filter_date_to": filter_date_to,
            "teams": Team.objects.all(),
            "movement_types": InstructorInventoryMovement.MovementType.choices,
            "inventory_manager": inventory_manager,
            "current_instructor": current_instructor,
            "administrator": is_operations_admin(request.user),
        },
    )


@login_required
def reports(request):
    if not is_operations_admin(request.user):
        raise PermissionDenied("Reporting is limited to administrator roles.")
    sessions = list(
        CourseSession.objects.select_related("course", "camp")
        .annotate(
            registration_total=Count("registrations", distinct=True),
            selected_total=Count(
                "registrations",
                filter=Q(registrations__selected_for_roster=True),
                distinct=True,
            ),
            passed_total=Count(
                "registrations",
                filter=Q(registrations__assessment_status="passed"),
                distinct=True,
            ),
            failed_total=Count(
                "registrations",
                filter=Q(registrations__assessment_status="failed"),
                distinct=True,
            ),
            pending_total=Count(
                "registrations",
                filter=Q(
                    registrations__assessment_status__in=[
                        "pending",
                        "absent",
                        "incomplete",
                    ]
                ),
                distinct=True,
            ),
            hp_total=Count(
                "registrations",
                filter=Q(registrations__is_hp=True),
                distinct=True,
            ),
            ttt_total=Count(
                "registrations",
                filter=Q(registrations__is_ttt=True),
                distinct=True,
            ),
        )
        .order_by("-start_date", "-id")
    )
    for session in sessions:
        session.display_reference = display_reference(session)
        session.has_saved_list = session.roster_snapshots.exists()
        session.has_stamped_list = session.stamped_list_archives.filter(active=True).exists()
        assignments = session.instructor_assignments.all()
        session.acknowledged_count = assignments.filter(acknowledged_at__isnull=False).count()
        session.instructor_count = assignments.count()

    report_view = request.GET.get("view", "progress")
    report_views = {
        "progress",
        "outcomes",
        "documents",
        "instructors",
        "inventory",
        "activity",
    }
    if report_view not in report_views:
        report_view = "progress"

    instructor_rows = []
    inventory_rows = []
    if report_view == "instructors":
        instructor_rows = list(
            CourseInstructor.objects.select_related(
                "session",
                "session__course",
                "session__camp",
                "instructor",
            ).order_by("-session__start_date", "instructor__name_english")
        )
        for assignment in instructor_rows:
            assignment.session.display_reference = display_reference(
                assignment.session
            )
    elif report_view == "inventory":
        inventory_rows = list(
            CourseInstructorInventoryUsage.objects.select_related(
                "session",
                "session__course",
                "session__camp",
                "instructor",
                "item",
            ).order_by(
                "-session__start_date",
                "instructor__name_english",
                "item__name",
            )
        )
        for usage in inventory_rows:
            usage.session.display_reference = display_reference(usage.session)

    return render(
        request,
        "portal/reports.html",
        {
            "sessions": sessions,
            "trained_students": TrainingRecord.objects.filter(
                result=TrainingRecord.Result.PASS
            )
            .exclude(session__status=CourseSession.Status.CANCELLED)
            .values("student_id")
            .distinct()
            .count(),
            "course_count": len(sessions),
            "completed_course_count": sum(
                session.status == CourseSession.Status.COMPLETED
                for session in sessions
            ),
            "open_registration_count": sum(
                session.status == CourseSession.Status.REGISTRATION_OPEN
                for session in sessions
            ),
            "report_view": report_view,
            "instructor_rows": instructor_rows,
            "inventory_rows": inventory_rows,
            "recent_activity": ActivityLog.objects.select_related("actor")[:200],
        },
    )


@login_required
def data_quality_workbench(request):
    if not is_operations_admin(request.user):
        raise PermissionDenied(
            "Data-quality oversight is limited to administrator roles."
        )
    registrations = list(
        Registration.objects.filter(requested_session__isnull=False)
        .select_related(
            "student",
            "requested_session",
            "requested_session__course",
            "requested_session__camp",
            "duplicate_reviewed_by",
        )
        .order_by("-submitted_at", "-id")
    )
    flagged_rows = [
        row
        for row in build_registration_review_rows(registrations)
        if row["review_required"]
    ]
    state_counts = {
        state: sum(row["review_state"] == state for row in flagged_rows)
        for state in ("required", "allowed", "excluded")
    }
    state_filter = request.GET.get("state", "required").strip()
    if state_filter not in {"all", "required", "allowed", "excluded"}:
        state_filter = "required"
    visible_rows = (
        flagged_rows
        if state_filter == "all"
        else [
            row
            for row in flagged_rows
            if row["review_state"] == state_filter
        ]
    )
    return render(
        request,
        "portal/data_quality_workbench.html",
        {
            "rows": visible_rows,
            "state_filter": state_filter,
            "state_counts": state_counts,
            "total_flagged": len(flagged_rows),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def people_teams(request):
    if not is_operations_admin(request.user):
        raise PermissionDenied("People and team management is limited to administrator roles.")
    if request.method == "POST":
        action = request.POST.get("action", "")
        try:
            with transaction.atomic():
                if action == "add_team":
                    name = request.POST.get("name", "").strip()
                    if not name:
                        raise ValidationError("Enter a team name.")
                    Team.objects.create(name=name)
                elif action == "add_role":
                    name = request.POST.get("name", "").strip()
                    if not name:
                        raise ValidationError("Enter a position name.")
                    InstructorRole.objects.create(name=name)
                elif action == "add_instructor":
                    name = request.POST.get("name", "").strip()
                    email = request.POST.get("email", "").strip().lower()
                    username = request.POST.get("username", "").strip()
                    password = request.POST.get("temporary_password", "")
                    if not all([name, email, username, password]):
                        raise ValidationError(
                            "Name, email, username and temporary password are required."
                        )
                    validate_email(email)
                    User = get_user_model()
                    user = User.objects.create_user(
                        username=username,
                        email=email,
                        password=password,
                    )
                    instructor = Instructor.objects.create(
                        user=user,
                        name_english=name,
                        email=email,
                        team_id=request.POST.get("team_id") or None,
                    )
                    role_id = request.POST.get("role_id")
                    if role_id:
                        instructor.roles.add(get_object_or_404(InstructorRole, pk=role_id))
                elif action == "update_instructor":
                    instructor = get_object_or_404(
                        Instructor, pk=request.POST.get("instructor_id")
                    )
                    name = request.POST.get("name", "").strip()
                    email = request.POST.get("email", "").strip().lower()
                    if not name:
                        raise ValidationError("Instructor name cannot be empty.")
                    if email:
                        validate_email(email)
                    instructor.name_english = name
                    instructor.email = email
                    instructor.team_id = request.POST.get("team_id") or None
                    instructor.is_inventory_supervisor = (
                        request.POST.get("is_inventory_supervisor") == "yes"
                    )
                    instructor.active = request.POST.get("active") == "yes"
                    instructor.save(
                        update_fields=[
                            "team",
                            "name_english",
                            "email",
                            "is_inventory_supervisor",
                            "active",
                            "updated_at",
                        ]
                    )
                    if instructor.user_id:
                        instructor.user.is_active = instructor.active
                        instructor.user.email = email
                        instructor.user.save(update_fields=["is_active", "email"])
                    role_ids = request.POST.getlist("role_ids")
                    instructor.roles.set(InstructorRole.objects.filter(id__in=role_ids))
                elif action == "set_team_leader":
                    team = get_object_or_404(Team, pk=request.POST.get("team_id"))
                    leader_id = request.POST.get("leader_id") or None
                    leader = (
                        get_object_or_404(Instructor, pk=leader_id)
                        if leader_id
                        else None
                    )
                    if leader is not None and leader.team_id != team.id:
                        leader.team = team
                        leader.save(update_fields=["team", "updated_at"])
                    team.leader = leader
                    team.save(update_fields=["leader", "updated_at"])
                elif action == "toggle_team":
                    team = get_object_or_404(Team, pk=request.POST.get("team_id"))
                    team.active = not team.active
                    team.save(update_fields=["active", "updated_at"])
                elif action == "toggle_role":
                    role = get_object_or_404(
                        InstructorRole, pk=request.POST.get("role_id")
                    )
                    role.active = not role.active
                    role.save(update_fields=["active", "updated_at"])
                else:
                    raise ValidationError("Choose a valid people or team action.")
        except Exception as exc:
            if isinstance(exc, ValidationError):
                errors = exc.messages
            else:
                errors = [str(exc)]
            for error in errors:
                messages.error(request, error)
        else:
            messages.success(request, "People and teams updated.")
        return redirect("people")
    return render(
        request,
        "portal/people_teams.html",
        {
            "teams": Team.objects.prefetch_related("instructors").all(),
            "instructors": Instructor.objects.select_related("team", "user").prefetch_related("roles"),
            "roles": InstructorRole.objects.all(),
        },
    )
