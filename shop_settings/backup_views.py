# SPDX-License-Identifier: AGPL-3.0-or-later
"""Admin-only backup actions. All mutations require POST and the normal CSRF middleware."""
import tempfile

from django import forms
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST, require_safe
from django.template.loader import render_to_string

from accounts.admin_mode import admin_mode_required
from . import backups
from .models import Backup, BackupSchedule


class ScheduleForm(forms.ModelForm):
    class Meta:
        model = BackupSchedule
        fields = ['enabled', 'time', 'retention']
        widgets = {'time': forms.TimeInput(format='%H:%M', attrs={'type': 'time'})}
        labels = {'enabled': 'Nightly backups', 'time': 'Time', 'retention': 'Scheduled backups to keep'}


def _backup(stamp):
    try:
        backups.checked_path(stamp)
    except backups.BackupError as exc:
        raise Http404 from exc
    return get_object_or_404(Backup, stamp=stamp)


@admin_mode_required
@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def index(request):
    schedule = backups.get_schedule()
    form, error, status = ScheduleForm(instance=schedule), '', 200
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'create':
                backups.create_backup('manual')
                return redirect('/settings/backups/?saved=backup')
            if action == 'schedule':
                form = ScheduleForm(request.POST, instance=schedule)
                if form.is_valid():
                    with backups.file_lock():
                        BackupSchedule.objects.filter(pk=1).update(**form.cleaned_data)
                    return redirect('/settings/backups/?saved=schedule')
            else:
                error = 'Unknown backup action.'
        except backups.BackupError as exc:
            error = str(exc)
        status = 400
    return render(request, 'settings/backups.html', {
        'title': 'Backups', 'backups': backups.list_backups(), 'form': form, 'schedule': schedule,
        'next_run': backups.next_run(schedule), 'error': error, 'saved': request.GET.get('saved'),
        'TIME_ZONE': str(backups.schedule_timezone()),
    }, status=status)


@admin_mode_required
@never_cache
@require_POST
def restore_copy(request, stamp):
    backup = _backup(stamp)
    try:
        backups.restore_to_copy(backup)
    except backups.BackupError as exc:
        return render(request, 'settings/backup-report.html', {'title': 'Restore report', 'backup': backup,
                      'report': backup.restore_report, 'error': str(exc)}, status=400)
    return redirect('backup-report', stamp=stamp)


@admin_mode_required
@never_cache
@require_POST
def discard_copy(request, stamp):
    backup = _backup(stamp)
    try:
        backups.discard_copy(backup)
    except backups.BackupError as exc:
        return render(request, 'settings/backup-report.html', {'title': 'Restore report', 'backup': backup,
                      'report': backup.restore_report, 'error': str(exc)}, status=400)
    return redirect('/settings/backups/?saved=discard')


@admin_mode_required
@never_cache
@require_safe
def report(request, stamp):
    backup = _backup(stamp)
    return render(request, 'settings/backup-report.html', {'title': 'Restore report', 'backup': backup,
                  'report': backup.restore_report, 'logs': backup.restore_logs.order_by('-created_at')})


@admin_mode_required
@never_cache
@require_POST
def switch(request, stamp):
    backup = _backup(stamp)
    try:
        if request.POST.get('confirmation') != 'RESTORE':
            raise backups.BackupError('Type RESTORE exactly to confirm switch-over.')
        backups.switch_over(backup, confirmed=True)
        return redirect('backup-report', stamp=stamp)
    except backups.BackupError as exc:
        if backups.control_path('MAINTENANCE').exists():
            # The live database may be unavailable. Never run request context processors
            # or refresh a model here (SQLite could create a new empty live file).
            return HttpResponse(render_to_string('settings/maintenance.html', {'error': str(exc)}), status=503,
                                headers={'Retry-After': '60', 'Cache-Control': 'no-store'})
        backup.refresh_from_db()
        return render(request, 'settings/backup-report.html', {'title': 'Restore report', 'backup': backup,
                      'report': backup.restore_report, 'error': str(exc)}, status=400)


@admin_mode_required
@never_cache
@require_safe
def download(request, stamp):
    backup = _backup(stamp)
    stream = tempfile.TemporaryFile()
    try:
        with backups.file_lock():
            backups.verify_backup(backup)
            digest = backups.write_download(backup, stream)
        stream.seek(0)
    except backups.BackupError as exc:
        stream.close()
        raise Http404(str(exc)) from exc
    except Exception:
        stream.close()
        raise
    response = FileResponse(stream, as_attachment=True, filename=f'freedomshop-backup-{stamp}.tar', content_type='application/x-tar')
    response['X-Checksum-SHA256'] = digest
    response['ETag'] = f'"{digest}"'
    return response
