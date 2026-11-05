# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notes & Requests screens (concept): list, detail with notes + attachments, new request, attachment bytes."""
import re

from django.db import transaction
from django.db.models import Count
from django.http import FileResponse, HttpResponse, HttpResponseNotFound, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import content_disposition_header
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from .models import ACCEPT_ATTR, KINDS, STATUS_ORDER, STATUSES, Attachment, Note, Request, check_upload, max_upload_text

NAME_SESSION_KEY = 'tracker_name'  # "your name", typed once per browser session
TITLE = 'Notes & Requests'
SHORTCUTS = [('/fixes/', 'Notes & Requests'), ('/fixes/new/', 'New Request'), ('/', 'Home')]


def remembered_name(request):
    session = getattr(request, 'session', None)
    return session.get(NAME_SESSION_KEY, '') if session is not None else ''


def remember_name(request, name):
    session = getattr(request, 'session', None)
    if session is not None and name:
        session[NAME_SESSION_KEY] = name


def clean_name(post):
    return post.get('name', '').strip()[:80]


def base_context(request, **extra):
    return dict(title=TITLE, shortcuts=SHORTCUTS, your_name=remembered_name(request), kinds=KINDS, statuses=STATUSES,
                max_upload=max_upload_text(), accept_files=ACCEPT_ATTR, **extra)


@require_safe
@never_cache
def index(request):
    show_done = request.GET.get('done') != '0'   # closed items stay visible (at the bottom); ?done=0 hides them
    rows = Request.objects.annotate(note_count=Count('notes', distinct=True), file_count=Count('attachments', distinct=True))
    if not show_done:
        rows = rows.exclude(status='done')
    rows = sorted(rows, key=lambda r: (STATUS_ORDER.get(r.status, 1), -r.updated_at.timestamp(), -r.pk))
    done_count = Request.objects.filter(status='done').count()
    return render(request, 'tracker/index.html', base_context(request, rows=rows, show_done=show_done, done_count=done_count,
                                                              saved=request.GET.get('saved', '')[:200]))


def save_attachment(req, uploaded, ctype, who, note=None):
    """Row first (for its pk), then the file streamed to disk in chunks; a failed write rolls the row back (caller's atomic)."""
    record = Attachment.objects.create(request=req, note=note, name=(uploaded.name or 'file')[:255], content_type=ctype,
                                       size=uploaded.size, uploaded_by=who)
    try:
        record.store(uploaded)
    except BaseException:
        record.remove_file()
        raise
    return record


@require_http_methods(['GET', 'POST'])
@never_cache
def new(request):
    form = dict(title='', kind='request', details='', name=remembered_name(request))
    error = ''
    if request.method == 'POST':
        form = dict(title=request.POST.get('title', '').strip()[:200], kind=request.POST.get('kind', 'request'),
                    details=request.POST.get('details', '').strip(), name=clean_name(request.POST))
        uploaded = request.FILES.get('file')
        ctype, error = check_upload(uploaded)
        if form['kind'] not in dict(KINDS):
            form['kind'] = 'request'
        if not form['title']:
            error = 'Give the request a short title.'
        if not error:
            with transaction.atomic():
                req = Request.objects.create(title=form['title'], kind=form['kind'], details=form['details'], created_by=form['name'], source=Request.next_number())
                if uploaded is not None:
                    save_attachment(req, uploaded, ctype, form['name'])
            remember_name(request, form['name'])
            return redirect(f'/fixes/{req.pk}/?saved=1')
    return render(request, 'tracker/new.html', base_context(request, form=form, error=error))


@require_http_methods(['GET', 'POST'])
@never_cache
def detail(request, pk):
    req = get_object_or_404(Request, pk=pk)
    error = ''
    message = 'Added to the list.' if request.GET.get('saved') == '1' else ''
    form = dict(text='', name=remembered_name(request))
    if request.method == 'POST':
        action = request.POST.get('action', 'note')
        if action == 'status':
            status = request.POST.get('status', '')
            if status in dict(STATUSES):
                if status != req.status:
                    req.status = status
                    req.save(update_fields=['status', 'updated_at'])
                return redirect(f'/fixes/{req.pk}/?status=1')
            error = 'Choose Open, Working or Done.'
        else:
            form = dict(text=request.POST.get('text', '').strip(), name=clean_name(request.POST))
            uploaded = request.FILES.get('file')
            ctype, error = check_upload(uploaded)
            if not error and not form['text'] and uploaded is None:
                error = 'Type a note or attach a file.'
            if not error:
                with transaction.atomic():
                    note = Note.objects.create(request=req, text=form['text'], author=form['name'])
                    if uploaded is not None:
                        save_attachment(req, uploaded, ctype, form['name'], note=note)
                    req.save(update_fields=['updated_at'])  # bump last activity
                remember_name(request, form['name'])
                return redirect(f'/fixes/{req.pk}/?noted=1#notes')
    if request.GET.get('status') == '1':
        message = f'Status set to {req.get_status_display()}.'
    elif request.GET.get('noted') == '1':
        message = 'Note added.'
    notes = list(req.notes.prefetch_related('attachments'))
    loose_files = list(req.attachments.filter(note=None))
    return render(request, 'tracker/detail.html', base_context(request, req=req, notes=notes, loose_files=loose_files, form=form,
                                                               error=error, message=message))


RANGE_RE = re.compile(r'^bytes=(\d*)-(\d*)$')


@require_safe
def attachment(request, pk):
    """Stream the file from disk; honours a single `Range: bytes=a-b` so <video> can seek (206 + Content-Range)."""
    record = get_object_or_404(Attachment, pk=pk)
    target = record.full_path
    if target is None or not target.is_file():
        return HttpResponseNotFound('This file is missing on the server.', content_type='text/plain')
    total = target.stat().st_size
    handle = open(target, 'rb')
    match = RANGE_RE.match(request.headers.get('Range', '').strip())
    if match and (match[1] or match[2]):
        if match[1]:
            start, end = int(match[1]), min(int(match[2]) if match[2] else total - 1, total - 1)
        else:  # suffix range: the last N bytes
            start, end = max(total - int(match[2]), 0), total - 1
        if start > end or start >= total:
            handle.close()
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{total}'
            return response
        handle.seek(start)
        response = StreamingHttpResponse(read_span(handle, end - start + 1), status=206, content_type=record.content_type)
        response['Content-Range'] = f'bytes {start}-{end}/{total}'
        response['Content-Length'] = str(end - start + 1)
    else:
        response = FileResponse(handle, content_type=record.content_type)
        response['Content-Length'] = str(total)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = content_disposition_header(not (record.is_image or record.is_video), record.name)
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, max-age=3600'
    return response


def read_span(handle, length, block=64 * 1024):
    try:
        while length > 0:
            chunk = handle.read(min(block, length))
            if not chunk:
                break
            length -= len(chunk)
            yield chunk
    finally:
        handle.close()
