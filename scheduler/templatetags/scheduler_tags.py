from django import template

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
