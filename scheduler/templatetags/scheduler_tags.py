from django import template
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter(name='dict_key')
def dict_key(d, key):
    """
    Looks up a dictionary key dynamically in templates.
    Usage: {{ mydict|dict_key:myvariable }}
    """
    if isinstance(d, dict):
        return d.get(key)
    return None

@register.filter(name='modulo_filter')
def modulo_filter(value, divisor):
    """
    Returns value % divisor — used for cycling CSS color classes.
    Usage: {{ course_id|modulo_filter:7 }}
    """
    try:
        return int(value) % int(divisor)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0


@register.filter(name='room_delivery_label')
def room_delivery_label(room):
    """
    Returns a human-readable delivery label for a room.
    Uses the explicit `is_virtual` flag on the Room model — no name-sniffing.
    """
    if not room:
        return 'Online'
    if room.is_virtual:
        return 'Online'
    if room.room_type == 'Lab':
        return 'Physical Lab'
    elif room.room_type == 'Lecture':
        return 'Lecture Hall'
    elif room.room_type == 'Seminar':
        return 'Seminar Room'
    return 'Physical Class'


@register.filter(name='delivery_badge')
def delivery_badge(course):
    """
    Returns a styled HTML pill badge for a course's delivery_mode field.
    Usage: {{ course|delivery_badge }}
    Choices: PH=Physical, OL=Online, HY=Hybrid
    """
    if not course:
        return ''
    mode = getattr(course, 'delivery_mode', 'PH')
    if mode == 'OL':
        return mark_safe(
            '<span style="display:inline-block;padding:0.15rem 0.55rem;border-radius:20px;'
            'font-size:0.72rem;font-weight:700;background:rgba(59,130,246,0.12);'
            'color:#1d4ed8;letter-spacing:0.04em;">🌐 Online</span>'
        )
    elif mode == 'HY':
        return mark_safe(
            '<span style="display:inline-block;padding:0.15rem 0.55rem;border-radius:20px;'
            'font-size:0.72rem;font-weight:700;background:rgba(168,85,247,0.12);'
            'color:#7c3aed;letter-spacing:0.04em;">⚡ Hybrid</span>'
        )
    else:  # PH — Physical (default)
        return mark_safe(
            '<span style="display:inline-block;padding:0.15rem 0.55rem;border-radius:20px;'
            'font-size:0.72rem;font-weight:700;background:rgba(34,197,94,0.12);'
            'color:#15803d;letter-spacing:0.04em;">🏫 Physical</span>'
        )
