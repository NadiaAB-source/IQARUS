
from pathlib import Path
import hashlib
import os
import uuid
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .access import require_session_access
from .models import ActivityLog, StampedListArchive, CourseSession


STAMPED_LISTS_ROOT = (
    Path(settings.BASE_DIR).parent / "stamped lists"
).resolve()
MAX_DOCUMENT_SIZE = 25 * 1024 * 1024


def _session(request, public_id):
    session = get_object_or_404(
        CourseSession.objects.select_related("course", "camp"),
        public_id=public_id,
    )
    require_session_access(request.user, session)
    return session


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip() or None
    return request.META.get("REMOTE_ADDR") or None


def _detected_file_type(upload):
    header = upload.read(16)
    upload.seek(0)

    if header.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"

    return None, None


@login_required
@require_http_methods(["GET", "POST"])
def stamped_lists(request, public_id):
    session = _session(request, public_id)

    if request.method == "POST":
        if session.status == CourseSession.Status.CANCELLED:
            messages.error(request, "A cancelled course is read-only.")
            return redirect("stamped_lists", public_id=session.public_id)
        upload = request.FILES.get("document")
        notes = request.POST.get("notes", "").strip()

        if upload is None:
            messages.error(request, "Choose a PDF, JPG or PNG document.")
            return redirect("stamped_lists", public_id=session.public_id)

        if upload.size <= 0:
            messages.error(request, "The selected document is empty.")
            return redirect("stamped_lists", public_id=session.public_id)

        if upload.size > MAX_DOCUMENT_SIZE:
            messages.error(request, "The document must not exceed 25 MB.")
            return redirect("stamped_lists", public_id=session.public_id)

        extension, content_type = _detected_file_type(upload)

        if extension is None:
            messages.error(
                request,
                "The file content is not a valid PDF, JPG or PNG document.",
            )
            return redirect("stamped_lists", public_id=session.public_id)

        original_name = Path(upload.name or "document").name[:255]
        relative_path = Path(
            str(session.public_id),
            uuid.uuid4().hex + extension,
        )
        final_path = (STAMPED_LISTS_ROOT / relative_path).resolve()

        if STAMPED_LISTS_ROOT not in final_path.parents:
            raise Http404("Invalid stamped-list archive path.")

        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = final_path.with_suffix(final_path.suffix + ".part")
        digest = hashlib.sha256()

        try:
            with open(temporary_path, "wb") as destination:
                for chunk in upload.chunks():
                    digest.update(chunk)
                    destination.write(chunk)
            os.replace(temporary_path, final_path)

            with transaction.atomic():
                StampedListArchive.objects.filter(
                    session=session,
                    active=True,
                ).update(active=False)

                document = StampedListArchive.objects.create(
                    session=session,
                    original_name=original_name,
                    stored_path=relative_path.as_posix(),
                    content_type=content_type,
                    size_bytes=upload.size,
                    sha256=digest.hexdigest(),
                    uploaded_by=request.user,
                    active=True,
                    notes=notes,
                )

                ActivityLog.objects.create(
                    actor=request.user,
                    action=ActivityLog.Action.UPDATE,
                    object_type="StampedListArchive",
                    object_id=str(document.public_id),
                    description="Stamped list archived for future reference.",
                    details={
                        "session_public_id": str(session.public_id),
                        "size_bytes": upload.size,
                        "sha256": document.sha256,
                    },
                    ip_address=_client_ip(request),
                )
        except Exception:
            temporary_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

        messages.success(
            request,
            "Stamped list archived successfully.",
        )
        return redirect("stamped_lists", public_id=session.public_id)

    documents = list(
        StampedListArchive.objects
        .filter(
            session=session,
        )
        .select_related("uploaded_by")
        .order_by("-uploaded_at", "-id")
    )

    return render(
        request,
        "portal/stamped_lists.html",
        {
            "active_module": "my_courses",
            "session": session,
            "documents": documents,
            "current_document": next(
                (document for document in documents if document.active),
                None,
            ),
            "course_read_only": session.status == CourseSession.Status.CANCELLED,
        },
    )


@login_required
def stamped_list_download(request, archive_id):
    document = get_object_or_404(
        StampedListArchive.objects.select_related("session"),
        public_id=archive_id,
    )
    require_session_access(request.user, document.session)

    file_path = (STAMPED_LISTS_ROOT / document.stored_path).resolve()

    if STAMPED_LISTS_ROOT not in file_path.parents:
        raise Http404("Invalid stamped-list archive path.")

    if not file_path.is_file():
        raise Http404("The archived stamped-list file is unavailable.")

    response = FileResponse(
        open(file_path, "rb"),
        content_type=document.content_type,
    )
    response["Content-Length"] = str(document.size_bytes)
    response["Content-Disposition"] = (
        "attachment; filename*=UTF-8''" + quote(document.original_name)
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response
