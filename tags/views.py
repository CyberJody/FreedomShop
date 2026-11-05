# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tag Generator screens: the tag-type page, saved Furniture Set Tags, the set editor, PDF and Direct Print,
and the Zebra Item Tag (pick an item, edit the text against a live preview, print to the tag printer)."""
from collections import Counter
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_safe

from sales.models import Item
from .models import DEFAULT_TAGLINE, FurnitureSet, FurnitureSetLine
from .sets import build, pdf_bytes

PRINTER_SESSION_KEY = 'report_printer'  # same remembered Letter printer as reports and documents
MAX_LINES = 30


def letter_printers():
    from reports.direct_print import letter_printers as printers
    return printers()


SET_PRINTER_SESSION_KEY = 'set_tag_printer'  # set tags remember their own printer


def remembered_printer(request):
    session = getattr(request, 'session', None)
    return session.get(PRINTER_SESSION_KEY, '') if session is not None else ''


def set_tag_printer(request, printers):
    """The printer preselected for Furniture Set Tags: the one last used for a set tag, else the first color Letter
    printer (the office Brother), else the shared remembered Letter printer."""
    session = getattr(request, 'session', None)
    chosen = session.get(SET_PRINTER_SESSION_KEY, '') if session is not None else ''
    if chosen and printers.filter(pk=chosen).exists():
        return chosen
    color = printers.filter(color=True).first()
    return str(color.pk) if color else remembered_printer(request)


@require_safe
@never_cache
def index(request):
    query = request.GET.get('q', '').strip()[:100]
    sets = FurnitureSet.objects.prefetch_related('lines__item')
    if query:
        sets = sets.filter(name__icontains=query)
    rows = [dict(tag_set=s, data=build(s)) for s in sets[:500]]
    return render(request, 'tags/index.html', dict(title='Tag Generator', rows=rows, query=query, deleted=request.GET.get('deleted', '')[:200]))


def parse_lines(post, kept=None, lock=False):
    """Rows posted by the editor: item ids and quantities, in screen order.

    `kept` = {item pk: how many SAVED lines of this set hold it}. A trashed item may stay on exactly those lines;
    a new line, or a second line, with a trashed item is refused. `lock=True` reads the Item rows FOR UPDATE, so the
    check and the save that follows it (one transaction in set_edit) can't be overtaken by a Delete in between."""
    kept = Counter(kept or {})
    items, quantities = post.getlist('item'), post.getlist('quantity')
    if len(items) != len(quantities):
        raise ValueError('The item list was not sent correctly. Reload the page and try again.')
    if not items:
        raise ValueError('Add at least one item to the set.')
    if len(items) > MAX_LINES:
        raise ValueError(f'A set tag can hold up to {MAX_LINES} items.')
    wanted = [int(k) for k in items if k.isdigit() and len(k) <= 18]
    if len(wanted) != len(items):
        raise ValueError('Choose items from the Item List.')
    queryset = Item.objects.filter(pk__in=wanted).order_by('pk')
    found = {i.pk: i for i in (queryset.select_for_update() if lock else queryset)}
    rows, trashed_used = [], Counter()
    for key, raw in zip(wanted, quantities):
        item = found.get(key)
        if item is None:
            raise ValueError('An item on this set is no longer in the Item List. Remove it and save again.')
        if not item.active:
            trashed_used[key] += 1
            if trashed_used[key] > kept[key]:
                raise ValueError(f'{item.display_name} is in the Items Trash. Restore it (Items Trash, admin sign-in) before adding it to a set.')
        try:
            quantity = Decimal(raw.strip())
        except (InvalidOperation, AttributeError):
            raise ValueError(f'Enter a quantity for {item.display_name}.')
        if not quantity.is_finite() or quantity <= 0 or quantity > 9999 or quantity != quantity.to_integral_value():
            raise ValueError(f'Quantity for {item.display_name} must be a whole number (1 to 9999).')
        rows.append((item, quantity))
    return rows


def editor_context(request, tag_set, error='', posted=None):
    if posted is not None:
        lines = [dict(item=i, quantity=q) for i, q in posted]
    elif tag_set.pk:
        lines = [dict(item=l.item, quantity=l.quantity, missing_name=l.item_name) for l in tag_set.lines.select_related('item')]
    else:
        keys = [int(k) for k in request.GET.getlist('item') if k.isdigit() and len(k) <= 18][:MAX_LINES]
        found = {i.pk: i for i in Item.objects.filter(pk__in=keys, active=True)}  # trashed items can't start a set
        lines = [dict(item=found[k], quantity=Decimal('1')) for k in dict.fromkeys(keys) if k in found]
    printers = letter_printers()
    trashed_lines = [line['item'].display_name for line in lines if line['item'] is not None and not line['item'].active]
    return dict(title='Furniture Set Tag', tag_set=tag_set, lines=lines, error=error, data=build(tag_set) if tag_set.pk else None, trashed_lines=trashed_lines,
                printers=printers, selected_printer=set_tag_printer(request, printers), saved=request.GET.get('saved') == '1',
                default_tagline=DEFAULT_TAGLINE, copy_choices=range(1, 6))


class SetError(ValueError):
    """A refused save; `rows` = the rows that did parse, so the editor can show them again."""

    def __init__(self, message, rows=None):
        super().__init__(message)
        self.rows = rows or []


def save_set(request, tag_set):
    """Validate and write the posted set in ONE transaction with the set and its items locked: a Delete that
    lands between the trashed-item check and the write waits for the lock and is seen, or waits for us."""
    name = request.POST.get('name', '').strip()
    tagline = request.POST.get('tagline', '').replace('\r\n', '\n').strip()
    copies = request.POST.get('copies', '1')
    tag_set.name, tag_set.tagline = name[:200], tagline
    tag_set.copies = int(copies) if copies.isdigit() and 1 <= int(copies) <= 5 else 1  # 1 copy by default
    with transaction.atomic():
        if tag_set.pk:
            FurnitureSet.objects.select_for_update().get(pk=tag_set.pk)
            kept = Counter(tag_set.lines.values_list('item_id', flat=True))
        else:
            kept = Counter()
        try:
            rows = parse_lines(request.POST, kept=kept, lock=True)
        except ValueError as exc:
            raise SetError(str(exc))
        if not name:
            raise SetError('Enter a name for this set, such as "Eagle Ridge dining set".', rows)
        if len(tagline) > 300:
            raise SetError('Keep the message under 300 characters.', rows)
        if len(tagline.splitlines()) > 4:
            raise SetError('Use up to 4 lines for the message.', rows)
        tag_set.save()
        tag_set.lines.all().delete()
        FurnitureSetLine.objects.bulk_create([FurnitureSetLine(tag_set=tag_set, position=n, item=item, sku=item.sku, item_name=item.display_name[:200],
                                                               description=item.description[:1000], quantity=quantity)
                                              for n, (item, quantity) in enumerate(rows)])
    return rows


@never_cache
@require_http_methods(['GET', 'HEAD', 'POST'])
def set_edit(request, token=None):
    tag_set = get_object_or_404(FurnitureSet, token=token) if token else FurnitureSet()
    if request.method != 'POST':
        return render(request, 'tags/set-edit.html', editor_context(request, tag_set))
    try:
        save_set(request, tag_set)
    except SetError as exc:
        return render(request, 'tags/set-edit.html', editor_context(request, tag_set, str(exc), exc.rows), status=400)
    return redirect(f'/tags/furniture-sets/{tag_set.token}/?saved=1' + ('&print=1' if request.POST.get('then') == 'print' else ''))


@require_safe
@never_cache
def set_pdf(request, token):
    tag_set = get_object_or_404(FurnitureSet, token=token)
    copies = request.GET.get('copies', '1')
    response = HttpResponse(pdf_bytes(tag_set, int(copies) if copies.isdigit() and 1 <= int(copies) <= 5 else 1), content_type='application/pdf')
    safe = ''.join(c if c.isalnum() else '-' for c in tag_set.name).strip('-')[:60] or 'Set'
    disposition = 'attachment' if request.GET.get('download') == '1' else 'inline'
    response['Content-Disposition'] = f'{disposition}; filename="Furniture-Set-Tag-{safe}.pdf"'
    return response


@never_cache
@require_http_methods(['POST'])
def set_print(request, token):
    """Send the tag (the chosen number of copies) to a Letter (Brother) printer."""
    from reports.direct_print import print_report
    from sales.printing import PrintError
    tag_set = get_object_or_404(FurnitureSet, token=token)
    key = request.POST.get('printer', '')
    printer = letter_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a printer.'}, status=400)
    copies = request.POST.get('copies', str(tag_set.copies))
    copies = int(copies) if copies.isdigit() and 1 <= int(copies) <= 5 else tag_set.copies
    try:
        message = print_report(printer, pdf_bytes(tag_set, copies), f'Furniture Set Tag {tag_set.name}')
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    if getattr(request, 'session', None) is not None:
        request.session[SET_PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': message})


@never_cache
@require_http_methods(['POST'])
def set_delete(request, token):
    from urllib.parse import urlencode
    tag_set = get_object_or_404(FurnitureSet, token=token)
    name = tag_set.name
    tag_set.delete()
    return redirect('/tags/?' + urlencode({'deleted': name}))


# ---------------------------------------------------------------- Item Tag (Zebra),

LABEL_PRINTER_SESSION_KEY = 'label_printer'


def label_printers():
    from sales.models import Printer
    return Printer.objects.filter(active=True, kind='label').order_by('name')


def item_tag_context(request, item, print_url):
    """Context for templates/tags/item-tag-editor.html (shared with the Item Price Tag page)."""
    from urllib.parse import urlencode
    from sales.label_printing import TagText, prefill
    text = prefill(item) if item is not None else TagText()
    session = getattr(request, 'session', None)
    return dict(item=item, tag_text=text, preview_query=urlencode(text.as_dict()), print_url=print_url,
                label_printers=label_printers(), label_printer=session.get(LABEL_PRINTER_SESSION_KEY, '') if session is not None else '')


@require_safe
@never_cache
def item_tag(request):
    key = request.GET.get('item', '')
    item = Item.objects.filter(pk=key, active=True).first() if key.isdigit() and len(key) <= 18 else None
    context = item_tag_context(request, item, '/tags/item-tag/print/')
    context.update(title='Item Tag', missing=bool(key) and item is None)
    return render(request, 'tags/item-tag.html', context)


@require_safe
@never_cache
def item_tag_preview(request):
    """PNG of the tag exactly as it prints (portrait). ?view=label shows it turned as the Zebra receives it."""
    from sales.label_printing import from_data, png_bytes, render_tag, to_label
    try:
        text = from_data(request.GET)
    except ValueError as exc:
        return HttpResponse(str(exc), status=400, content_type='text/plain; charset=utf-8')
    image = render_tag(text)
    if request.GET.get('view') == 'label':
        image = to_label(image)
    response = HttpResponse(png_bytes(image), content_type='image/png')
    response['X-Content-Type-Options'] = 'nosniff'
    return response


def print_label_tag(request, fallback=None):
    """Shared by the Tag Generator and the Item Price Tag page: validate, send to the Zebra, remember the printer.
    Returns a JsonResponse."""
    from sales.label_printing import from_data, text_zpl
    from sales.printing import PrintError, send
    key = request.POST.get('printer', '')
    printer = label_printers().filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
    if printer is None:
        return JsonResponse({'error': 'Choose a tag printer.'}, status=400)
    copies = request.POST.get('copies', '1')
    if not copies.isdigit() or len(copies) > 2 or not 1 <= int(copies) <= 10:
        return JsonResponse({'error': 'Choose 1 to 10 copies.'}, status=400)
    copies = int(copies)
    try:
        text = from_data(request.POST, fallback)
    except ValueError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    if not (text.name or text.code or text.price):
        return JsonResponse({'error': 'Enter a name, item code or price for the tag.'}, status=400)
    try:
        send(printer, text_zpl(text, copies))
    except PrintError as exc:
        return JsonResponse({'error': str(exc)}, status=502)
    session = getattr(request, 'session', None)
    if session is not None:
        session[LABEL_PRINTER_SESSION_KEY] = str(printer.pk)
    return JsonResponse({'message': f'Sent {copies} tag{"s" if copies != 1 else ""} to {printer.name}.'})


@never_cache
@require_http_methods(['POST'])
def item_tag_print(request):
    from sales.label_printing import prefill
    key = request.POST.get('item', '')
    item = None
    if key:
        item = Item.objects.filter(pk=key).first() if key.isdigit() and len(key) <= 18 else None
        if item is None:
            return JsonResponse({'error': 'That item is no longer in the Item List.'}, status=400)
        if not item.print_tags:
            return JsonResponse({'error': 'Print Tags is off for this item. Enable it in Edit Item before printing.'}, status=400)
    return print_label_tag(request, prefill(item) if item is not None else None)
