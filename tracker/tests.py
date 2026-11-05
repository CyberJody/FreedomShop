# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notes & Requests: list, new request with a file, notes with image attachments, status, home tile."""
import struct
import zlib

from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.utils.datastructures import MultiValueDict

from .models import Attachment, Note, Request
from .views import detail


class SizedUpload(SimpleUploadedFile):
    """A small upload that reports a huge size, so the cap can be tested without allocating hundreds of MB."""
    def __init__(self, name, content, content_type, size):
        super().__init__(name, content, content_type=content_type)
        self._fake_size = size

    @property
    def size(self):
        return self._fake_size

    @size.setter
    def size(self, value):
        pass


def post_with_file(url, data, uploaded, view, **kwargs):
    """POST through the view directly with `uploaded` placed in request.FILES (the test client would re-measure it)."""
    request = RequestFactory().post(url, data)
    request.POST  # parse the body first, then swap in the upload
    request._files = MultiValueDict({'file': [uploaded]})
    return view(request, **kwargs)


def tiny_png():
    """A 2×2 red PNG built by hand (no PIL needed)."""
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)
    raw = b''.join(b'\x00' + b'\xff\x00\x00' * 2 for _ in range(2))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class FixesAndRequestsTests(TestCase):
    def test_new_request_with_a_file_redirects_to_detail_and_remembers_the_name(self):
        response = self.client.post('/fixes/new/', {'title': 'Receipt prints twice', 'kind': 'fix', 'details': 'On the west counter.',
                                                    'name': 'Morgan', 'file': SimpleUploadedFile('note.txt', b'twice', content_type='text/plain')})
        req = Request.objects.get()
        self.assertEqual(response['Location'], f'/fixes/{req.pk}/?saved=1')
        self.assertEqual((req.title, req.kind, req.status, req.created_by), ('Receipt prints twice', 'fix', 'open', 'Morgan'))
        self.assertEqual(req.source, 'R11')  # The initial range remains reserved for compatibility.
        file = req.attachments.get()
        self.assertEqual((file.name, file.content_type, file.size, file.uploaded_by, file.note), ('note.txt', 'text/plain', 5, 'Morgan', None))
        detail = self.client.get(f'/fixes/{req.pk}/?saved=1')
        self.assertContains(detail, 'Added to the list.')
        self.assertContains(detail, 'Receipt prints twice')
        self.assertContains(detail, f'href="/fixes/attachments/{file.pk}/"')
        self.assertContains(detail, 'value="Morgan"')  # remembered in the session
        self.assertContains(self.client.get('/fixes/new/'), 'value="Morgan"')
        self.assertContains(self.client.get('/fixes/'), '<td>Morgan</td>')
        blank = self.client.post('/fixes/new/', {'title': '  ', 'kind': 'request'})
        self.assertContains(blank, 'Give the request a short title.')
        self.assertEqual(Request.objects.count(), 1)

    def test_note_with_image_attachment_is_served_back_as_png(self):
        req = Request.objects.create(title='Gold button', created_by='Pat')
        png = tiny_png()
        response = self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'text': 'Here is the screen', 'name': 'Morgan',
                                                          'file': SimpleUploadedFile('shot.png', png, content_type='image/png')})
        self.assertEqual(response['Location'], f'/fixes/{req.pk}/?noted=1#notes')
        note = Note.objects.get()
        self.assertEqual((note.text, note.author), ('Here is the screen', 'Morgan'))
        file = Attachment.objects.get()
        self.assertEqual((file.note, file.request, file.content_type, file.size), (note, req, 'image/png', len(png)))
        served = self.client.get(f'/fixes/attachments/{file.pk}/')
        self.assertEqual(served['Content-Type'], 'image/png')
        self.assertEqual(served_bytes(served), png)
        self.assertIn('inline', served['Content-Disposition'])
        detail = self.client.get(f'/fixes/{req.pk}/?noted=1')
        self.assertContains(detail, 'Note added.')
        self.assertContains(detail, f'<img src="/fixes/attachments/{file.pk}/"')
        self.assertContains(detail, 'Here is the screen')
        # a note can be just a file; neither text nor file is refused
        self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'name': 'Morgan', 'file': SimpleUploadedFile('b.png', png, content_type='image/png')})
        self.assertEqual(Note.objects.count(), 2)
        empty = self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'text': '  ', 'name': 'Morgan'})
        self.assertContains(empty, 'Type a note or attach a file.')
        self.assertEqual(Note.objects.count(), 2)
        listed = self.client.get('/fixes/')
        self.assertContains(listed, '<td class="number">2</td><td class="number">2</td>')
        # non-image files download with their name
        self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'text': 'pdf', 'file': SimpleUploadedFile('spec.pdf', b'%PDF-1.4 x', content_type='application/pdf')})
        pdf = Attachment.objects.get(name='spec.pdf')
        served = self.client.get(f'/fixes/attachments/{pdf.pk}/')
        self.assertEqual(served['Content-Type'], 'application/pdf')
        self.assertIn('attachment; filename="spec.pdf"', served['Content-Disposition'])
        self.assertContains(self.client.get(f'/fixes/{req.pk}/'), 'spec.pdf')

    def test_oversized_and_unsupported_files_are_refused(self):
        req = Request.objects.create(title='Gold button')
        big = SizedUpload('huge.mp4', b'x' * 10, content_type='video/mp4', size=300 * 1024 * 1024)
        response = post_with_file(f'/fixes/{req.pk}/', {'action': 'note', 'text': 'big'}, big, detail, pk=req.pk)
        self.assertContains(response, '&quot;huge.mp4&quot; is 300.0 MB; attachments can be up to 250 MB.')
        self.assertEqual(list(Path(settings.TRACKER_UPLOAD_DIR).rglob('*huge*')), [])  # nothing written to disk
        exe = SimpleUploadedFile('tool.exe', b'MZ', content_type='application/octet-stream')
        response = self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'text': 'exe', 'file': exe})
        self.assertContains(response, 'not a file type this list accepts')
        response = self.client.post('/fixes/new/', {'title': 'New', 'file': SimpleUploadedFile('run.sh', b'#!', content_type='text/x-sh')})
        self.assertContains(response, 'not a file type this list accepts')
        self.assertEqual((Note.objects.count(), Attachment.objects.count(), Request.objects.count()), (0, 0, 1))
        # a browser that sends a generic type still gets through on a known extension
        ok = self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'file': SimpleUploadedFile('x.jpeg', b'\xff\xd8', content_type='application/octet-stream')})
        self.assertEqual(ok.status_code, 302)
        self.assertEqual(Attachment.objects.get().content_type, 'image/jpeg')

    def test_status_change_bumps_last_activity_and_reorders(self):
        older = Request.objects.create(title='Older', status='working')
        newer = Request.objects.create(title='Newer')
        before = older.updated_at
        self.assertEqual([r.pk for r in self.client.get('/fixes/').context['rows']], [newer.pk, older.pk])
        response = self.client.post(f'/fixes/{older.pk}/', {'action': 'status', 'status': 'done'})
        self.assertEqual(response['Location'], f'/fixes/{older.pk}/?status=1')
        older.refresh_from_db()
        self.assertEqual(older.status, 'done')
        self.assertGreater(older.updated_at, before)
        done_page = self.client.get(f'/fixes/{older.pk}/?status=1')
        self.assertContains(done_page, 'Status set to Done.')
        self.assertContains(done_page, 'id="reopen-form"')  # closed items offer Reopen and still take notes
        self.assertContains(done_page, 'notes are still welcome')
        self.client.post(f'/fixes/{older.pk}/', {'action': 'note', 'text': 'Still broken on the east register', 'name': 'Morgan'})
        self.assertContains(self.client.get(f'/fixes/{older.pk}/'), 'Still broken on the east register')
        self.client.post(f'/fixes/{older.pk}/', {'action': 'status', 'status': 'open'})  # what the Reopen button posts
        older.refresh_from_db()
        self.assertEqual(older.status, 'open')
        self.assertNotContains(self.client.get(f'/fixes/{older.pk}/'), 'id="reopen-form"')
        self.client.post(f'/fixes/{older.pk}/', {'action': 'status', 'status': 'done'})
        self.assertEqual([r.pk for r in self.client.get('/fixes/').context['rows']], [newer.pk, older.pk])  # done stays listed, at the bottom
        self.client.post(f'/fixes/{older.pk}/', {'action': 'status', 'status': 'working'})
        self.assertEqual([r.pk for r in self.client.get('/fixes/').context['rows']], [older.pk, newer.pk])  # newest activity first
        bad = self.client.post(f'/fixes/{older.pk}/', {'action': 'status', 'status': 'closed'})
        self.assertContains(bad, 'Choose Open, Working or Done.')
        self.assertEqual(self.client.get('/fixes/999/').status_code, 404)

    def test_home_has_the_gold_shortcut_last_in_the_button_list_and_menu_entry(self):
        home = self.client.get('/')
        self.assertNotContains(home, 'class="task col-1 gold"')  # off the flowchart, under the button list
        self.assertContains(home, '<a class="shortcut gold" href="/fixes/"><svg class="shortcut-icon" aria-hidden="true"><use href="#fixes"/></svg>Notes &amp; Requests</a>')
        self.assertContains(home, '<symbol id="fixes"')
        body = home.content.decode()
        self.assertLess(body.index('href="/pos/order/new/">Sales Order</a>'), body.index('class="shortcut gold"'))  # after Sales Order
        self.assertContains(self.client.get('/fixes/'), '<a href="/fixes/">Notes &amp; Requests</a>')  # Point of Sale menu


def served_bytes(response):
    return b''.join(response.streaming_content)


@override_settings(MIDDLEWARE=['django.contrib.sessions.middleware.SessionMiddleware'])
class AttachmentFilesOnDiskTests(TestCase):
    """attachments are files under TRACKER_UPLOAD_DIR (a temp dir in tests), capped at 250 MB; videos play inline."""

    def add_note_file(self, req, name, payload, content_type):
        response = self.client.post(f'/fixes/{req.pk}/', {'action': 'note', 'text': name, 'name': 'Pat',
                                                          'file': SimpleUploadedFile(name, payload, content_type=content_type)})
        self.assertEqual(response.status_code, 302)
        return Attachment.objects.get(name=name)

    def test_one_megabyte_mp4_is_written_to_disk_and_served_with_ranges(self):
        req = Request.objects.create(title='Screen recording')
        payload = bytes(range(256)) * 4096  # 1 MB
        file = self.add_note_file(req, 'clip 1.mp4', payload, 'application/octet-stream')  # type from the extension
        self.assertEqual((file.content_type, file.size, file.path), ('video/mp4', len(payload), f'{req.pk}/{file.pk}-clip_1.mp4'))
        on_disk = Path(settings.TRACKER_UPLOAD_DIR) / file.path
        self.assertTrue(on_disk.is_file())
        self.assertEqual(on_disk.read_bytes(), payload)
        self.assertIn('fs-tracker-test-', str(on_disk))  # a temp dir, never the repo
        whole = self.client.get(f'/fixes/attachments/{file.pk}/')
        self.assertEqual((whole.status_code, whole['Content-Type'], whole['Accept-Ranges']), (200, 'video/mp4', 'bytes'))
        self.assertEqual(whole['Content-Length'], str(len(payload)))
        self.assertIn('inline', whole['Content-Disposition'])
        self.assertEqual(served_bytes(whole), payload)
        part = self.client.get(f'/fixes/attachments/{file.pk}/', HTTP_RANGE='bytes=0-99')
        self.assertEqual(part.status_code, 206)
        self.assertEqual(part['Content-Range'], f'bytes 0-99/{len(payload)}')
        self.assertEqual(part['Content-Length'], '100')
        self.assertEqual(served_bytes(part), payload[:100])
        tail = self.client.get(f'/fixes/attachments/{file.pk}/', HTTP_RANGE='bytes=1048000-')
        self.assertEqual((tail.status_code, tail['Content-Range']), (206, f'bytes 1048000-{len(payload) - 1}/{len(payload)}'))
        self.assertEqual(served_bytes(tail), payload[1048000:])
        self.assertEqual(self.client.get(f'/fixes/attachments/{file.pk}/', HTTP_RANGE=f'bytes={len(payload)}-').status_code, 416)
        page = self.client.get(f'/fixes/{req.pk}/')
        self.assertContains(page, f'<video controls preload="metadata" src="/fixes/attachments/{file.pk}/"></video>')
        self.assertContains(page, 'up to 250 MB')
        self.assertContains(self.client.get('/fixes/new/'), 'Attach a file (screenshot, video, PDF; up to 250 MB)')

    def test_image_still_serves_inline_and_renders_as_img(self):
        req = Request.objects.create(title='Screenshot')
        png = tiny_png()
        file = self.add_note_file(req, 'shot.png', png, 'image/png')
        served = self.client.get(f'/fixes/attachments/{file.pk}/')
        self.assertEqual((served['Content-Type'], served_bytes(served)), ('image/png', png))
        self.assertIn('inline', served['Content-Disposition'])
        page = self.client.get(f'/fixes/{req.pk}/')
        self.assertContains(page, f'<img src="/fixes/attachments/{file.pk}/"')
        self.assertNotContains(page, '<video')

    def test_deleting_the_attachment_removes_the_file_and_a_missing_file_is_a_plain_404(self):
        req = Request.objects.create(title='Cleanup')
        file = self.add_note_file(req, 'gone.webm', b'webm-bytes', 'video/webm')
        on_disk = file.full_path
        self.assertTrue(on_disk.is_file())
        with self.captureOnCommitCallbacks(execute=True):
            file.delete()
        self.assertFalse(on_disk.exists())
        other = self.add_note_file(req, 'lost.mov', b'mov-bytes', 'video/quicktime')
        other.full_path.unlink()
        missing = self.client.get(f'/fixes/attachments/{other.pk}/')
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.content, b'This file is missing on the server.')
        with self.captureOnCommitCallbacks(execute=True):  # cascade from the request deletes files too
            third = self.add_note_file(req, 'third.txt', b'abc', 'text/plain')
        path = third.full_path
        with self.captureOnCommitCallbacks(execute=True):
            req.delete()
        self.assertFalse(path.exists())


class AttachmentMigrationTests(TransactionTestCase):
    """tracker 0002 writes existing attachment bytes to files and drops the column."""

    def test_existing_bytes_move_to_disk(self):
        executor = MigrationExecutor(connection)
        executor.migrate([('tracker', '0001_initial')])
        old = executor.loader.project_state([('tracker', '0001_initial')]).apps
        req = old.get_model('tracker', 'Request').objects.create(title='Old')
        row = old.get_model('tracker', 'Attachment').objects.create(request=req, name='old shot.png', content_type='image/png',
                                                                      data=b'old-bytes', size=9)
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([('tracker', '0002_attachment_files_on_disk')])
        moved = Attachment.objects.get(pk=row.pk)
        self.assertEqual((moved.path, moved.size), (f'{req.pk}/{row.pk}-old_shot.png', 9))
        self.assertEqual(moved.full_path.read_bytes(), b'old-bytes')
        moved.full_path.unlink()
