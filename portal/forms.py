from django import forms
from django.db.models import Q

from .models import Camp, Course, CourseSession, Instructor


class CourseCreationForm(forms.Form):
    course = forms.ModelChoiceField(
        queryset=Course.objects.none(),
        empty_label="Select a course",
    )
    camp = forms.ModelChoiceField(
        queryset=Camp.objects.none(),
        empty_label="Select a camp",
    )
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    end_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    capacity = forms.IntegerField(min_value=1, max_value=500)
    instructor_student_ratio = forms.IntegerField(
        label="Students per instructor",
        min_value=1,
        max_value=100,
        help_text="Example: enter 10 for a 1:10 instructor-to-student ratio.",
    )
    service_branch = forms.ChoiceField(
        label="Service Branch",
        choices=[("", "Select a service branch")]
        + list(CourseSession.ServiceBranch.choices),
    )
    service_branch_other = forms.CharField(
        label="Other service branch",
        max_length=100,
        required=False,
    )
    instructors = forms.ModelMultipleChoiceField(
        queryset=Instructor.objects.none(),
        label="Assigned instructors",
        required=True,
        widget=forms.CheckboxSelectMultiple(
            attrs={"class": "instructor-checkbox-list"}
        ),
        help_text=(
            "Select one or more instructors for this course. Only the selected "
            "instructors will receive the course email and see the course."
        ),
    )
    poc_name = forms.CharField(label="Point of Contact name", max_length=200)
    poc_contact_number = forms.CharField(
        label="Point of Contact number",
        max_length=80,
    )
    poc_location_url = forms.URLField(
        label="Location link",
        max_length=500,
        help_text="Paste the map or location link.",
    )

    def __init__(self, *args, session=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = session
        course_filter = Q(active=True)
        camp_filter = Q(active=True)
        instructor_filter = Q(active=True)
        if session is not None:
            course_filter |= Q(pk=session.course_id)
            if session.camp_id:
                camp_filter |= Q(pk=session.camp_id)
            instructor_filter |= Q(
                course_assignments__session=session
            )
        self.fields["course"].queryset = Course.objects.filter(
            course_filter
        ).distinct()
        self.fields["camp"].queryset = Camp.objects.filter(
            camp_filter
        ).distinct()
        self.fields["instructor_student_ratio"].widget.attrs[
            "aria-describedby"
        ] = "ratio-help"
        self.fields["instructors"].widget.attrs[
            "aria-describedby"
        ] = "instructors-help"
        self.fields["poc_location_url"].widget.attrs[
            "aria-describedby"
        ] = "location-help"
        active_instructors = Instructor.objects.filter(
            instructor_filter
        ).distinct().order_by("name_english")
        self.fields["instructors"].queryset = active_instructors

    def clean(self):
        cleaned = super().clean()
        start_date = cleaned.get("start_date")
        end_date = cleaned.get("end_date")
        if start_date and end_date and end_date < start_date:
            self.add_error("end_date", "End date cannot be before the start date.")

        service_branch = cleaned.get("service_branch")
        service_branch_other = (
            cleaned.get("service_branch_other") or ""
        ).strip()
        if service_branch == CourseSession.ServiceBranch.OTHER:
            if not service_branch_other:
                self.add_error(
                    "service_branch_other",
                    "Enter the service branch.",
                )
        else:
            service_branch_other = ""
        cleaned["service_branch_other"] = service_branch_other

        selected = list(cleaned.get("instructors") or [])
        for instructor in selected:
            email = (
                instructor.email
                or (instructor.user.email if instructor.user_id else "")
            ).strip()
            if not instructor.user_id:
                self.add_error(
                    "instructors",
                    f"{instructor.name_english} does not have a login account.",
                )
            if not email:
                self.add_error(
                    "instructors",
                    f"{instructor.name_english} needs an email address before assignment.",
                )
        cleaned["selected_instructors"] = selected
        return cleaned
