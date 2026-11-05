# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notes & Requests (concept): the list of fixes and requests the store is working on, with a notes thread
and file attachments (screenshots, PDFs, screen-recording videos). Attachment files live on disk under
settings.TRACKER_UPLOAD_DIR (path stored relative to it); the row keeps name, type and size."""
import re
from pathlib import Path

from django.conf import settings
from django.db import models, transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

KINDS = [('fix', 'Fix'), ('request', 'Request')]
STATUSES = [('open', 'Open'), ('working', 'Working'), ('done', 'Done')]
STATUS_ORDER = {'open': 0, 'working': 0, 'done': 1}  # open and working first, done at the bottom


def max_upload_bytes():
    return settings.TRACKER_MAX_UPLOAD


def max_upload_text():
    return f'{max_upload_bytes() // (1024 * 1024)} MB'


def upload_root():
    return Path(settings.TRACKER_UPLOAD_DIR)


ALLOWED_CONTENT_TYPES = {
    'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
    'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp', 'image/bmp': '.bmp',
    'application/pdf': '.pdf', 'text/plain': '.txt', 'text/csv': '.csv',
    'application/msword': '.doc', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
    'application/vnd.ms-excel': '.xls', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-powerpoint': '.ppt', 'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
    'application/vnd.oasis.opendocument.text': '.odt', 'application/vnd.oasis.opendocument.spreadsheet': '.ods',
    'application/rtf': '.rtf',
}
ALLOWED_EXTENSIONS = {ext: ctype for ctype, ext in ALLOWED_CONTENT_TYPES.items()}
ALLOWED_EXTENSIONS['.jpeg'] = 'image/jpeg'
ALLOWED_EXTENSIONS['.text'] = 'text/plain'
ALLOWED_EXTENSIONS['.md'] = 'text/plain'
ALLOWED_EXTENSIONS['.m4v'] = 'video/mp4'
ACCEPT_ATTR = 'image/*,video/mp4,video/quicktime,video/webm,.mp4,.mov,.webm,.m4v,.pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx,.odt,.ods,.rtf'


def safe_name(name):
    """A file-system-safe version of an uploaded name (letters, digits, dot, dash, underscore)."""
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(name or 'file').name).strip('._') or 'file'
    return base[-120:]


class Request(models.Model):
    title = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=KINDS, default='request')
    status = models.CharField(max_length=10, choices=STATUSES, default='open')
    details = models.TextField(blank=True)
    created_by = models.CharField(max_length=80, blank=True)
    source = models.CharField(max_length=40, blank=True, help_text='The request number, e.g. R15 — the same number used in the request batch notes')

    @property
    def number(self):
        """The request number everyone refers to."""
        return self.source or f'#{self.pk}'

    @staticmethod
    def next_number():
        """Next free request number, retaining the reserved initial range for compatibility."""
        highest = 10
        for value in Request.objects.exclude(source='').values_list('source', flat=True):
            m = re.fullmatch(r'R(\d+)', value.strip())
            if m:
                highest = max(highest, int(m.group(1)))
        return f'R{highest + 1}'
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)  # last activity: any note, file or status change touches it

    class Meta:
        ordering = ['-updated_at', '-pk']

    def __str__(self):
        return self.title

    @property
    def is_done(self):
        return self.status == 'done'


class Note(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name='notes')
    text = models.TextField(blank=True)
    author = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']


class Attachment(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name='attachments')
    note = models.ForeignKey(Note, on_delete=models.SET_NULL, null=True, blank=True, related_name='attachments')
    name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    path = models.CharField(max_length=300, blank=True, help_text='relative to settings.TRACKER_UPLOAD_DIR')
    size = models.PositiveIntegerField(default=0)
    uploaded_by = models.CharField(max_length=80, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at', 'pk']

    def __str__(self):
        return self.name

    @property
    def is_image(self):
        return self.content_type.startswith('image/')

    @property
    def is_video(self):
        return self.content_type.startswith('video/')

    @property
    def full_path(self):
        return upload_root() / self.path if self.path else None

    def store(self, uploaded):
        """Write the upload to disk in chunks (never the whole file in memory) and set path/size. Needs a pk."""
        relative = f'{self.request_id}/{self.pk}-{safe_name(self.name)}'
        target = upload_root() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with open(target, 'wb') as out:
            for chunk in uploaded.chunks():
                out.write(chunk)
                size += len(chunk)
        self.path, self.size = relative, size
        self.save(update_fields=['path', 'size'])

    def remove_file(self):
        target = self.full_path
        if target is not None:
            target.unlink(missing_ok=True)
            try:
                target.parent.rmdir()  # the request's folder, once empty
            except OSError:
                pass

    @property
    def size_text(self):
        size = self.size or 0
        if size >= 1024 * 1024:
            return f'{size / (1024 * 1024):.1f} MB'
        if size >= 1024:
            return f'{size // 1024} KB'
        return f'{size} bytes'


def check_upload(uploaded):
    """Return (content_type, error) for an uploaded file: the type we store, or a plain message when refused."""
    if uploaded is None:
        return None, None
    name = uploaded.name or ''
    ext = ('.' + name.rsplit('.', 1)[-1].lower()) if '.' in name else ''
    ctype = (uploaded.content_type or '').split(';')[0].strip().lower()
    if ctype not in ALLOWED_CONTENT_TYPES:
        ctype = ALLOWED_EXTENSIONS.get(ext, '')
    if not ctype:
        return None, f'"{name}" is not a file type this list accepts. Attach an image, video (MP4, MOV, WebM), PDF, text or Office file.'
    if uploaded.size > max_upload_bytes():
        return None, f'"{name}" is {uploaded.size / (1024 * 1024):.1f} MB; attachments can be up to {max_upload_text()}.'
    if uploaded.size == 0:
        return None, f'"{name}" is empty.'
    return ctype, None


@receiver(post_delete, sender=Attachment)
def delete_attachment_file(sender, instance, **kwargs):
    """Remove the file from disk once the row's deletion is committed (also on cascades from Request/queryset deletes)."""
    transaction.on_commit(instance.remove_file)
