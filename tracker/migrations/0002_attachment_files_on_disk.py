# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixes and Requests attachments move from a BinaryField to files on disk (settings.TRACKER_UPLOAD_DIR).
Adds `path`, writes any existing bytes to <request pk>/<attachment pk>-<safe name>, then drops `data`. Safe with 0 rows."""
import re
from pathlib import Path

from django.conf import settings
from django.db import migrations, models


def safe_name(name):  # frozen copy of tracker.models.safe_name
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', Path(name or 'file').name).strip('._') or 'file'
    return base[-120:]


def bytes_to_files(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('SET CONSTRAINTS ALL IMMEDIATE')  # no pending trigger events before the RemoveField ALTER
    Attachment = apps.get_model('tracker', 'Attachment')
    root = Path(settings.TRACKER_UPLOAD_DIR)
    for record in Attachment.objects.filter(path='').only('pk', 'request_id', 'name').iterator():
        data = bytes(Attachment.objects.values_list('data', flat=True).get(pk=record.pk) or b'')
        relative = f'{record.request_id}/{record.pk}-{safe_name(record.name)}'
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        Attachment.objects.filter(pk=record.pk).update(path=relative, size=len(data))


def files_to_bytes(apps, schema_editor):
    Attachment = apps.get_model('tracker', 'Attachment')
    root = Path(settings.TRACKER_UPLOAD_DIR)
    for record in Attachment.objects.exclude(path='').only('pk', 'path').iterator():
        target = root / record.path
        data = target.read_bytes() if target.is_file() else b''
        Attachment.objects.filter(pk=record.pk).update(data=data)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='attachment',
            name='path',
            field=models.CharField(blank=True, help_text='relative to settings.TRACKER_UPLOAD_DIR', max_length=300),
        ),
        migrations.AlterField(model_name='attachment', name='data', field=models.BinaryField(default=b'')),
        migrations.RunPython(bytes_to_files, files_to_bytes),
        migrations.RemoveField(model_name='attachment', name='data'),
    ]
