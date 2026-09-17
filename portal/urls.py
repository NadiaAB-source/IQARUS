from django.urls import path
from . import views, training_views, course_views, registration_views, public_registration_views, stamped_list_views, course_workflow_views, external_upload_views

urlpatterns = [

    path(
        "register/<uuid:token>/",
        public_registration_views.course_registration,
        name="course_registration",
    ),
    path(
        "my-courses/<uuid:public_id>/registration-qr/image/",
        public_registration_views.registration_qr_image,
        name="registration_qr_image",
    ),
    path(
        "my-courses/<uuid:public_id>/registration-qr/",
        public_registration_views.registration_qr,
        name="registration_qr",
    ),

    path(
        "my-courses/<uuid:public_id>/external-upload.xlsx",
        external_upload_views.external_upload_xlsx,
        name="external_upload_xlsx",
    ),
    path(
        "my-courses/<uuid:public_id>/stamped-lists/",
        stamped_list_views.stamped_lists,
        name="stamped_lists",
    ),
    path(
        "my-courses/stamped-lists/<uuid:archive_id>/download/",
        stamped_list_views.stamped_list_download,
        name="stamped_list_download",
    ),
    path(
        "my-courses/new/",
        course_workflow_views.create_course,
        name="course_create",
    ),
    path(
        "my-courses/<uuid:public_id>/acknowledge/",
        course_workflow_views.acknowledge_course,
        name="course_acknowledge",
    ),
    path(
        "my-courses/<uuid:public_id>/edit/",
        course_workflow_views.edit_course,
        name="course_edit",
    ),
    path(
        "my-courses/<uuid:public_id>/cancel/",
        course_workflow_views.cancel_course,
        name="course_cancel",
    ),
    path(
        "my-courses/<uuid:public_id>/delete/",
        course_workflow_views.delete_course,
        name="course_delete",
    ),
    path(
        "my-courses/<uuid:public_id>/external-upload-complete/",
        course_workflow_views.confirm_external_upload,
        name="external_upload_complete",
    ),
    path(
        "my-courses/<uuid:public_id>/resend-course-information/",
        course_workflow_views.resend_course_information,
        name="resend_course_information",
    ),
    path(
        "my-courses/<uuid:public_id>/save-roster/",
        course_workflow_views.save_course_roster,
        name="course_roster_save",
    ),
    path(
        "my-courses/<uuid:public_id>/save-and-download-list/",
        course_workflow_views.save_and_download_roster,
        name="course_roster_save_download",
    ),
    path(
        "my-courses/saved-lists/<uuid:snapshot_id>/download/",
        course_workflow_views.download_saved_roster,
        name="saved_roster_download",
    ),
    path(
        "my-courses/<uuid:public_id>/equipment/",
        course_workflow_views.save_course_inventory,
        name="course_inventory_save",
    ),
    path(
        "my-courses/<uuid:public_id>/",
        course_workflow_views.course_workspace,
        name="instructor_course_workspace",
    ),
    path("my-courses/", course_workflow_views.my_courses, name="my_courses"),
    path("registrations/<uuid:public_id>/", registration_views.registration_detail, name="registration_detail"),
    path("registrations/", registration_views.registration_list, name="registrations_active"),
    path("courses/", course_views.course_session_list, name="courses_active"),
    path("courses/", course_views.course_session_list, name="courses_workspace"),
    path(
        "courses-sessions/<uuid:public_id>/",
        course_views.course_session_detail,
        name="course_session_detail",
    ),
    path(
        "courses-sessions/",
        course_views.course_session_list,
        name="courses_sessions",
    ),
    path("training-records/", training_views.training_record_list, name="training_records"),
    path("training-records/print/", training_views.training_record_print, name="training_records_print"),
    path("", views.dashboard, name="dashboard"),
    path(
        "students/",
        views.student_list,
        name="students",
    ),
    path(
        "students/print/",
        views.student_print,
        name="students_print",
    ),
    path(
        "students/<uuid:public_id>/",
        views.student_detail,
        name="student_detail",
    ),
    path("courses-overview/", course_views.course_session_list, name="courses"),
    path("registrations-overview/", registration_views.registration_list, name="registrations"),
    path(
        "schedule/",
        views.module_page,
        {"module_key": "schedule"},
        name="schedule",
    ),
    path(
        "people/",
        course_workflow_views.people_teams,
        name="people",
    ),
    path(
        "inventory/",
        course_workflow_views.inventory,
        name="inventory",
    ),
    path(
        "reports/",
        course_workflow_views.reports,
        name="reports",
    ),
    path(
        "data-quality/",
        course_workflow_views.data_quality_workbench,
        name="data_quality",
    ),
]
