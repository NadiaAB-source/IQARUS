from io import BytesIO
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from . import course_workflow_views
from .course_services import (
    day_one_list_filename,
    day_one_students_upload_filename,
)
from .identity import format_emirates_id, masked_emirates_id
from .models import (
    ActivityLog,
    Camp,
    Course,
    CourseInstructor,
    CourseInstructorInventoryUsage,
    CourseSession,
    Instructor,
    InstructorInventoryBalance,
    InstructorInventoryMovement,
    InstructorRole,
    InventoryItem,
    Registration,
    Student,
    TrainingRecord,
)


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    DEFAULT_FROM_EMAIL="test@iqarus.example",
)
class SimpleCourseWorkflowTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            username="lead-admin",
            password="test-password",
            is_staff=True,
        )
        self.instructor_user = User.objects.create_user(
            username="course-instructor",
            email="instructor@example.com",
            password="test-password",
        )
        lead_role = InstructorRole.objects.create(name="LI")
        self.instructor = Instructor.objects.create(
            user=self.instructor_user,
            name_english="COURSE INSTRUCTOR",
            email="instructor@example.com",
        )
        self.instructor.roles.add(lead_role)
        self.unassigned_user = User.objects.create_user(
            username="unassigned-instructor",
            email="unassigned@example.com",
            password="test-password",
        )
        instructor_role = InstructorRole.objects.create(name="INS")
        self.unassigned_instructor = Instructor.objects.create(
            user=self.unassigned_user,
            name_english="UNASSIGNED INSTRUCTOR",
            email="unassigned@example.com",
        )
        self.unassigned_instructor.roles.add(instructor_role)
        self.course = Course.objects.create(
            code="ASM",
            title_english="ASM - TCCC",
        )
        self.camp = Camp.objects.create(name="Test Camp")
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_roster_root = course_workflow_views.ROSTER_ROOT
        course_workflow_views.ROSTER_ROOT = Path(self.tempdir.name)

    def tearDown(self):
        course_workflow_views.ROSTER_ROOT = self.original_roster_root
        self.tempdir.cleanup()

    def _create_course(self, instructor_ids=None):
        self.client.force_login(self.admin)
        instructor_ids = instructor_ids or [self.instructor.id]
        response = self.client.post(
            reverse("course_create"),
            {
                "course": self.course.id,
                "camp": self.camp.id,
                "start_date": "2026-09-01",
                "end_date": "2026-09-02",
                "capacity": "20",
                "instructor_student_ratio": "10",
                "service_branch": "PG",
                "instructors": instructor_ids,
                "poc_name": "Camp Contact",
                "poc_contact_number": "+971500000000",
                "poc_location_url": "https://example.com/location",
            },
        )
        self.assertEqual(response.status_code, 302)
        return CourseSession.objects.get(course=self.course)

    def _registration(
        self,
        session,
        *,
        eid="784198012345678",
        name_english="TEST STUDENT",
        name_arabic="طالب تجريبي",
        email="student@example.com",
    ):
        return Registration.objects.create(
            requested_session=session,
            submitted_name_english=name_english,
            submitted_name_arabic=name_arabic,
            eid_raw=eid,
            eid_normalized=eid,
            email_raw=email,
            submitted_unit="Unit",
            status=Registration.Status.PENDING,
        )

    def test_emirates_id_display_always_uses_dashes(self):
        raw = "784198012345678"
        expected = "784-1980-1234567-8"
        self.assertEqual(format_emirates_id(raw), expected)
        self.assertEqual(format_emirates_id(expected), expected)
        self.assertEqual(
            masked_emirates_id(raw),
            "•••-••••-••••567-8",
        )

    def test_create_course_uses_assignment_focused_layout(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("course_create"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Course details")
        self.assertContains(response, "Service Branch")
        self.assertContains(response, "Point of Contact")
        self.assertContains(response, "Assigned instructors")
        self.assertContains(response, 'id="instructor-search"')
        self.assertContains(response, 'id="selected-instructor-count"')
        self.assertNotContains(response, "Lead Instructor")

    def test_course_creation_emails_assignment_and_creates_qr_token(self):
        session = self._create_course()
        assignment = CourseInstructor.objects.get(session=session)
        self.assertEqual(
            assignment.assignment_role,
            CourseInstructor.AssignmentRole.INSTRUCTOR,
        )
        self.assertIsNotNone(assignment.notified_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Camp Contact", mail.outbox[0].body)
        self.assertIn("Service Branch: PG", mail.outbox[0].body)
        self.assertIn("https://example.com/location", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, ["instructor@example.com"])
        self.assertFalse(
            CourseInstructor.objects.filter(
                session=session,
                instructor=self.unassigned_instructor,
            ).exists()
        )
        self.assertTrue(session.public_registration_token)
        self.assertFalse(session.registration_published)

        self.client.force_login(self.unassigned_user)
        response = self.client.get(
            reverse("instructor_course_workspace", args=[session.public_id])
        )
        self.assertEqual(response.status_code, 403)

    def test_course_can_have_multiple_selected_instructors(self):
        session = self._create_course(
            [self.instructor.id, self.unassigned_instructor.id]
        )
        self.assertEqual(
            CourseInstructor.objects.filter(session=session).count(),
            2,
        )
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(
            {message.to[0] for message in mail.outbox},
            {"instructor@example.com", "unassigned@example.com"},
        )

    def test_other_service_branch_requires_and_saves_custom_name(self):
        self.client.force_login(self.admin)
        course_data = {
            "course": self.course.id,
            "camp": self.camp.id,
            "start_date": "2026-09-01",
            "end_date": "2026-09-02",
            "capacity": "20",
            "instructor_student_ratio": "10",
            "service_branch": "OTHER",
            "instructors": [self.instructor.id],
            "poc_name": "Camp Contact",
            "poc_contact_number": "+971500000000",
            "poc_location_url": "https://example.com/location",
        }
        response = self.client.post(reverse("course_create"), course_data)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enter the service branch.")

        course_data["service_branch_other"] = "Coast Guard"
        response = self.client.post(reverse("course_create"), course_data)
        self.assertEqual(response.status_code, 302)
        session = CourseSession.objects.get(course=self.course)
        self.assertEqual(session.service_branch, "OTHER")
        self.assertEqual(session.service_branch_other, "Coast Guard")
        self.assertEqual(session.service_branch_name, "Coast Guard")
        self.assertIn("Service Branch: Coast Guard", mail.outbox[0].body)

    def test_authorised_user_edits_course_and_only_new_assignment_is_emailed(self):
        session = self._create_course()
        assignment = CourseInstructor.objects.get(session=session)
        assignment.acknowledged_at = timezone.now()
        assignment.save(update_fields=["acknowledged_at", "updated_at"])
        second_camp = Camp.objects.create(name="Second Camp")
        mail.outbox.clear()

        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("course_edit", args=[session.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Save Course Changes")

        response = self.client.post(
            reverse("course_edit", args=[session.public_id]),
            {
                "course": self.course.id,
                "camp": second_camp.id,
                "start_date": "2026-09-03",
                "end_date": "2026-09-04",
                "capacity": "30",
                "instructor_student_ratio": "15",
                "service_branch": "NAVY",
                "instructors": [self.unassigned_instructor.id],
                "poc_name": "Updated Contact",
                "poc_contact_number": "+971511111111",
                "poc_location_url": "https://example.com/updated-location",
            },
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.camp, second_camp)
        self.assertEqual(session.capacity, 30)
        self.assertEqual(session.service_branch, "NAVY")
        self.assertEqual(session.poc_name, "Updated Contact")
        self.assertTrue(session.reference_code.endswith("SECOND-CAMP"))
        self.assertFalse(
            CourseInstructor.objects.filter(
                session=session,
                instructor=self.instructor,
            ).exists()
        )
        new_assignment = CourseInstructor.objects.get(
            session=session,
            instructor=self.unassigned_instructor,
        )
        self.assertIsNone(new_assignment.acknowledged_at)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["unassigned@example.com"])
        self.assertIn("Updated Contact", mail.outbox[0].body)
        activity = ActivityLog.objects.get(
            action=ActivityLog.Action.UPDATE,
            object_id=str(session.public_id),
            description="Course details or instructor assignments updated.",
        )
        self.assertEqual(
            activity.details["added_instructor_ids"],
            [self.unassigned_instructor.id],
        )
        self.assertEqual(
            activity.details["removed_instructor_ids"],
            [self.instructor.id],
        )

    def test_course_cancel_preserves_course_and_writes_activity_log(self):
        session = self._create_course()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("course_cancel", args=[session.public_id]),
            {"reason": "Camp requested cancellation"},
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertEqual(session.status, CourseSession.Status.CANCELLED)
        self.assertFalse(session.registration_published)
        activity = ActivityLog.objects.get(
            action=ActivityLog.Action.CANCEL,
            object_id=str(session.public_id),
        )
        self.assertEqual(
            activity.details["reason"],
            "Camp requested cancellation",
        )

    def test_empty_course_can_be_deleted_but_activity_remains(self):
        session = self._create_course()
        public_id = str(session.public_id)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("course_delete", args=[session.public_id]),
            {"confirm": "delete", "reason": "Created by mistake"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CourseSession.objects.filter(public_id=public_id).exists())
        activity = ActivityLog.objects.get(
            action=ActivityLog.Action.DELETE,
            object_id=public_id,
        )
        self.assertEqual(activity.details["reason"], "Created by mistake")

    def test_course_with_registration_cannot_be_deleted(self):
        session = self._create_course()
        self._registration(session)
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("course_delete", args=[session.public_id]),
            {"confirm": "delete"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(CourseSession.objects.filter(pk=session.pk).exists())
        self.assertFalse(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.DELETE,
                object_id=str(session.public_id),
            ).exists()
        )

    def test_inventory_is_held_and_reported_per_instructor(self):
        item = InventoryItem.objects.create(
            name="Training Kit",
            category=InventoryItem.Category.CONSUMABLE,
            unit="kit",
            quantity_on_hand=20,
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("inventory"),
            {
                "action": "allocate",
                "instructor_id": self.unassigned_instructor.id,
                "item_id": item.id,
                "movement_type": "issue",
                "quantity": "8",
            },
        )
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.quantity_on_hand, 12)
        balance = InstructorInventoryBalance.objects.get(
            instructor=self.unassigned_instructor,
            item=item,
        )
        self.assertEqual(balance.quantity_on_hand, 8)

        session = self._create_course(
            [self.instructor.id, self.unassigned_instructor.id]
        )
        self.client.force_login(self.unassigned_user)
        response = self.client.post(
            reverse("course_inventory_save", args=[session.public_id]),
            {
                "instructor_id": self.unassigned_instructor.id,
                f"used_{item.id}": "5",
                f"consumed_{item.id}": "2",
                f"deteriorated_{item.id}": "1",
                f"inventory_notes_{item.id}": "Course use",
            },
        )
        self.assertEqual(response.status_code, 302)
        balance.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, 5)
        usage = CourseInstructorInventoryUsage.objects.get(
            session=session,
            instructor=self.unassigned_instructor,
            item=item,
        )
        self.assertEqual(usage.quantity_used, 5)
        self.assertEqual(usage.quantity_consumed, 2)
        self.assertEqual(usage.quantity_deteriorated, 1)
        movement = InstructorInventoryMovement.objects.filter(
            session=session,
            instructor=self.unassigned_instructor,
            item=item,
        ).latest("created_at")
        self.assertEqual(movement.quantity_change, -3)
        self.assertEqual(movement.balance_after, 5)

        # Saving the same report again must not deduct the balance twice.
        response = self.client.post(
            reverse("course_inventory_save", args=[session.public_id]),
            {
                "instructor_id": self.unassigned_instructor.id,
                f"used_{item.id}": "5",
                f"consumed_{item.id}": "2",
                f"deteriorated_{item.id}": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        balance.refresh_from_db()
        self.assertEqual(balance.quantity_on_hand, 5)

        response = self.client.post(
            reverse("course_inventory_save", args=[session.public_id]),
            {"instructor_id": self.instructor.id},
        )
        self.assertEqual(response.status_code, 403)

    def test_inventory_records_use_tabs_filters_and_25_row_pages(self):
        for number in range(26):
            item = InventoryItem.objects.create(
                name=f"Filter Item {number:02d}",
                category=InventoryItem.Category.EQUIPMENT,
                unit="item",
                quantity_on_hand=0,
            )
            InstructorInventoryBalance.objects.create(
                instructor=self.instructor,
                item=item,
                quantity_on_hand=number + 1,
                updated_by=self.admin,
            )
            InstructorInventoryMovement.objects.create(
                instructor=self.instructor,
                item=item,
                movement_type=InstructorInventoryMovement.MovementType.ISSUE,
                quantity_change=number + 1,
                balance_after=number + 1,
                recorded_by=self.admin,
            )

        self.client.force_login(self.admin)
        response = self.client.get(reverse("inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "portal/js/scroll_position.")
        self.assertContains(response, 'id="inventory-balances-panel"')
        self.assertNotContains(response, 'id="inventory-history-panel"')
        self.assertEqual(len(response.context["balance_page"]), 25)
        self.assertTrue(response.context["balance_page"].has_next())

        response = self.client.get(
            reverse("inventory"),
            {"view": "balances", "q": "Filter Item 25"},
        )
        self.assertEqual(response.context["balance_page"].paginator.count, 1)

        response = self.client.get(
            reverse("inventory"),
            {"view": "history"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="inventory-history-panel"')
        self.assertNotContains(response, 'id="inventory-balances-panel"')
        self.assertEqual(len(response.context["movement_page"]), 25)
        self.assertTrue(response.context["movement_page"].has_next())

        response = self.client.get(
            reverse("inventory"),
            {"view": "history", "page": "2"},
        )
        self.assertEqual(len(response.context["movement_page"]), 1)

    def test_assigned_instructor_acknowledges_and_controls_registration(self):
        session = self._create_course()
        self.client.force_login(self.instructor_user)
        response = self.client.post(
            reverse("course_acknowledge", args=[session.public_id])
        )
        self.assertEqual(response.status_code, 302)
        assignment = CourseInstructor.objects.get(session=session)
        self.assertIsNotNone(assignment.acknowledged_at)
        response = self.client.post(
            reverse("registration_qr", args=[session.public_id]),
            {"action": "open"},
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertTrue(session.registration_published)
        response = self.client.post(
            reverse("registration_qr", args=[session.public_id]),
            {"action": "close"},
        )
        self.assertEqual(response.status_code, 302)
        session.refresh_from_db()
        self.assertFalse(session.registration_published)

    def test_single_roster_saves_independent_tags_and_exact_template(self):
        session = self._create_course()
        registration = self._registration(session)
        self._registration(
            session,
            eid="784198112345679",
            name_english="NOT SELECTED STUDENT",
            name_arabic="طالب غير مختار",
            email="not-selected@example.com",
        )
        self.client.force_login(self.instructor_user)
        response = self.client.post(
            reverse("course_roster_save_download", args=[session.public_id]),
            {
                f"selected_{registration.public_id}": "yes",
                f"hp_{registration.public_id}": "yes",
                f"ttt_{registration.public_id}": "yes",
                f"result_{registration.public_id}": "passed",
                f"remarks_{registration.public_id}": "Clear practical result",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            "attachment; filename*=UTF-8''01-02_September_COURSE_Test_Camp.xlsx",
        )
        registration.refresh_from_db()
        self.assertTrue(registration.selected_for_roster)
        self.assertTrue(registration.is_hp)
        self.assertTrue(registration.is_ttt)
        self.assertEqual(registration.assessment_status, "passed")
        self.assertEqual(TrainingRecord.objects.count(), 1)
        record = TrainingRecord.objects.get()
        self.assertTrue(record.is_hp)
        self.assertTrue(record.is_ttt)

        workbook = load_workbook(BytesIO(response.content), data_only=False)
        self.assertEqual(workbook.sheetnames, ["List 1-1"])
        sheet = workbook.active
        self.assertEqual(
            [sheet.cell(3, column).value for column in range(1, 9)],
            [
                "#",
                "Email",
                "EID / رقم الهوية",
                "اسم الطالب",
                "Student Name",
                "Day 1",
                "Day 2",
                "Status",
            ],
        )
        self.assertEqual(sheet["B4"].value, "student@example.com")
        self.assertEqual(sheet["C4"].value, "784-1980-1234567-8")
        self.assertEqual(sheet["D4"].value, "طالب تجريبي")
        self.assertEqual(sheet["E4"].value, "TEST STUDENT")
        self.assertIsNone(sheet["H4"].value)
        self.assertEqual(sheet["E5"].value, "Status: Pass, Fail, D (Duplicate), TTT (Train the Trainer)")
        self.assertIsNone(sheet["A5"].value)
        self.assertTrue(str(sheet.print_area).endswith("$H$7"))
        self.assertNotIn(
            0,
            [
                cell.value
                for row in sheet.iter_rows(min_row=1, max_row=7)
                for cell in row
            ],
        )
        response = self.client.get(
            reverse("instructor_course_workspace", args=[session.public_id])
        )
        self.assertContains(response, "784-1980-1234567-8")
        self.assertNotContains(response, "Review required")

    def test_workspace_bulk_controls_are_in_their_table_columns(self):
        session = self._create_course()
        self._registration(session)
        self.client.force_login(self.instructor_user)

        response = self.client.get(
            reverse("instructor_course_workspace", args=[session.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'class="column-control">Select')
        self.assertContains(response, 'id="hp-all"')
        self.assertContains(response, 'id="ttt-all"')
        self.assertContains(response, 'id="pass-all"')
        self.assertContains(response, 'id="fail-all"')
        self.assertContains(response, 'class="student-row')
        self.assertContains(response, ".roster{min-width:0;table-layout:fixed}")
        self.assertNotContains(response, 'class="toolbar"')

    def test_existing_student_and_previous_same_course_require_review(self):
        session = self._create_course()
        master = Student.objects.create(
            eid="784198012345678",
            name_english="TEST STUDENT",
            name_arabic="طالب تجريبي",
            email="student@example.com",
        )
        earlier_session = CourseSession.objects.create(
            course=self.course,
            camp=self.camp,
            reference_code="ASM-PREVIOUS",
            start_date="2026-08-01",
            end_date="2026-08-02",
            status=CourseSession.Status.COMPLETED,
        )
        TrainingRecord.objects.create(
            student=master,
            session=earlier_session,
            result=TrainingRecord.Result.PASS,
        )
        registration = self._registration(session)
        self.client.force_login(self.instructor_user)

        response = self.client.get(
            reverse("instructor_course_workspace", args=[session.public_id])
        )
        self.assertContains(response, "Existing master student")
        self.assertContains(response, "Previously attended this course")
        self.assertContains(response, "Review required")

        response = self.client.post(
            reverse("course_roster_save", args=[session.public_id]),
            {
                f"selected_{registration.public_id}": "yes",
                f"result_{registration.public_id}": "pending",
            },
        )
        self.assertEqual(response.status_code, 302)
        registration.refresh_from_db()
        self.assertFalse(registration.selected_for_roster)

        response = self.client.post(
            reverse("course_roster_save", args=[session.public_id]),
            {
                f"selected_{registration.public_id}": "yes",
                f"duplicate_decision_{registration.public_id}": "reviewed_allow",
                f"duplicate_notes_{registration.public_id}": "Identity checked",
                f"result_{registration.public_id}": "pending",
            },
        )
        self.assertEqual(response.status_code, 302)
        registration.refresh_from_db()
        self.assertTrue(registration.selected_for_roster)
        self.assertEqual(
            registration.duplicate_review_status,
            Registration.DuplicateReviewStatus.ALLOWED,
        )
        self.assertEqual(registration.duplicate_review_notes, "Identity checked")
        self.assertEqual(len(registration.duplicate_review_fingerprint), 64)
        self.assertEqual(registration.duplicate_reviewed_by, self.instructor_user)

        self.client.force_login(self.admin)
        response = self.client.get(reverse("data_quality") + "?state=allowed")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Identity checked")
        self.assertContains(response, "Previously attended this course")

    def test_external_upload_contains_only_passed_selected_student(self):
        session = self._create_course()
        registration = self._registration(session)
        self.client.force_login(self.instructor_user)
        self.client.post(
            reverse("course_roster_save", args=[session.public_id]),
            {
                f"selected_{registration.public_id}": "yes",
                f"result_{registration.public_id}": "passed",
            },
        )
        response = self.client.get(
            reverse("external_upload_xlsx", args=[session.public_id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Disposition"],
            "attachment; filename*=UTF-8''"
            "01-02_September_COURSE_Test_Camp_Students%20upload.xlsx",
        )
        workbook = load_workbook(BytesIO(response.content), data_only=False)
        self.assertEqual(workbook.sheetnames, ["obs", "TCCC ASM"])
        sheet = workbook["TCCC ASM"]
        self.assertEqual(
            [sheet.cell(1, column).value for column in range(1, 6)],
            ["email", "emirates_id", "full_name", "session1", "session2"],
        )
        self.assertEqual(sheet["C2"].value, "طالب تجريبي")
        self.assertEqual(sheet["B2"].value, "784-1980-1234567-8")
        self.assertEqual(sheet["D2"].value, "YES")
        self.assertEqual(sheet["E2"].value, "YES")

    def test_day_one_filenames_match_supplied_examples(self):
        session = self._create_course()
        self.instructor.name_english = "Vasilios Example"
        self.instructor.save(update_fields=["name_english", "updated_at"])
        self.unassigned_instructor.name_english = "Abdulsattar Dalgamuni"
        self.unassigned_instructor.save(
            update_fields=["name_english", "updated_at"]
        )
        CourseInstructor.objects.create(
            session=session,
            instructor=self.unassigned_instructor,
        )
        session.start_date = timezone.datetime(2026, 8, 19).date()
        session.end_date = timezone.datetime(2026, 8, 20).date()
        self.camp.name = "Specialized Reserve Nahel PG"
        self.camp.save(update_fields=["name", "updated_at"])

        expected_base = (
            "19-20_August_Vasilios-Abdulsattar_"
            "Specialized_Reserve_Nahel_PG"
        )
        self.assertEqual(day_one_list_filename(session), expected_base + ".xlsx")
        self.assertEqual(
            day_one_students_upload_filename(session),
            expected_base + "_Students upload.xlsx",
        )

    def test_course_lists_use_compact_human_references(self):
        session = self._create_course()
        self.client.force_login(self.admin)

        response = self.client.get(reverse("my_courses"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "course-row")
        self.assertContains(response, "View Course")
        self.assertContains(response, "Camp")
        self.assertContains(response, "Test Camp")
        self.assertContains(response, 'class="instructor-tag"')
        self.assertContains(response, 'class="course-instructors-label">Instructors</strong>')
        self.assertContains(response, "COURSE INSTRUCTOR")
        self.assertNotContains(response, "ASM · TCCC · 01 SEP 2026")
        self.assertNotContains(response, session.reference_code)

        response = self.client.get(reverse("courses_sessions"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MASTER DIRECTORY")
        self.assertContains(response, "ASM - TCCC")
        self.assertContains(response, "Course Directory")

    def test_reports_offer_focused_one_row_views(self):
        session = self._create_course()
        registration = self._registration(session)
        registration.selected_for_roster = True
        registration.assessment_status = "passed"
        registration.is_hp = True
        registration.save(
            update_fields=[
                "selected_for_roster",
                "assessment_status",
                "is_hp",
                "updated_at",
            ]
        )
        item = InventoryItem.objects.create(
            name="Report Kit",
            category=InventoryItem.Category.EQUIPMENT,
            unit="kit",
        )
        CourseInstructorInventoryUsage.objects.create(
            session=session,
            instructor=self.instructor,
            item=item,
            quantity_used=2,
        )
        self.client.force_login(self.admin)

        for view_name, expected in [
            ("progress", "Course progress"),
            ("outcomes", "Student outcomes"),
            ("documents", "Document status"),
            ("instructors", "COURSE INSTRUCTOR"),
            ("inventory", "Report Kit"),
            ("activity", "Activity log"),
        ]:
            response = self.client.get(
                reverse("reports") + f"?view={view_name}"
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, expected)

    def test_student_directory_print_uses_day_one_browser_sheet(self):
        student = Student.objects.create(
            eid="784198012345678",
            name_english="PRINT TEST STUDENT",
            name_arabic="طالب للطباعة",
            email="print@example.com",
        )
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("students_print"),
            {
                "mode": "selected",
                "student_ids": [student.pk],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Student Directory List")
        self.assertContains(response, "EID / رقم الهوية")
        self.assertContains(response, "Day 1")
        self.assertContains(response, "Day 2")
        self.assertContains(response, "Status")
        self.assertContains(response, "PRINT TEST STUDENT")
        self.assertTrue(
            ActivityLog.objects.filter(
                action=ActivityLog.Action.PRINT,
                object_type="Student",
            ).exists()
        )

    def test_admin_navigation_exposes_course_directory(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Course Directory", count=2)
        self.assertContains(response, "iqarus-logo-transparent")

        self.client.force_login(self.unassigned_user)
        response = self.client.get(reverse("courses_sessions"))
        self.assertEqual(response.status_code, 403)
