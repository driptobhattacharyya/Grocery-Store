"""Fixed HTML e-mail templates. Everything dynamic goes through Jinja2 autoescaping
and nothing is visitor-typed free text: product names come from the sandbox (and are
escaped), the rest is numbers, dates and ids."""
from jinja2 import Environment

_env = Environment(autoescape=True)

SITE = "grocery-store.driptobhattacharyya.com"
FOOTER = (
    "You're receiving this because you entered this address on the Grocery Store portfolio demo "
    f"({SITE}). It was sent once, on request. No mailing list."
)

_BASE = """\
<!doctype html>
<html><body style="margin:0;padding:0;background:#f4f6f4;font-family:Arial,Helvetica,sans-serif;color:#1f2a1f">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:8px">
<tr><td style="padding:20px 24px;background:#2e7d32;color:#ffffff;font-size:18px;font-weight:bold;border-radius:8px 8px 0 0">Grocery Store (demo)</td></tr>
<tr><td style="padding:24px;font-size:15px;line-height:1.5">
{% block body %}{% endblock %}
</td></tr>
<tr><td style="padding:16px 24px;font-size:12px;color:#6b776b;border-top:1px solid #e3e8e3">{{ footer }}</td></tr>
</table></td></tr></table>
</body></html>
"""

_ORDER = _BASE.replace("{% block body %}{% endblock %}", """\
<p style="margin-top:0">Thanks for your order!</p>
<p>Order <strong>#{{ order_id }}</strong></p>
<table role="presentation" width="100%" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr style="text-align:left;border-bottom:1px solid #e3e8e3"><th>Item</th><th>Qty</th><th style="text-align:right">Price</th></tr>
{% for item in items %}<tr style="border-bottom:1px solid #f0f2f0"><td>{{ item.name }}</td><td>{{ item.quantity }}</td><td style="text-align:right">{{ '%.2f' % item.line_total }} INR</td></tr>
{% endfor %}<tr><td colspan="2"><strong>Total</strong></td><td style="text-align:right"><strong>{{ '%.2f' % total }} INR</strong></td></tr>
</table>
<p>This is a demo store: nothing will be shipped.</p>""")

_REPORT = _BASE.replace("{% block body %}{% endblock %}", """\
<p style="margin-top:0">Here is your monthly report for <strong>{{ account }}</strong> (demo data).</p>
{% if orders %}<p>You ordered <strong>{{ '%.2f' % total }} INR</strong> in {{ orders|length }} order{{ '' if orders|length == 1 else 's' }} over the last 30 days:</p>
<table role="presentation" width="100%" cellpadding="6" cellspacing="0" style="border-collapse:collapse">
<tr style="text-align:left;border-bottom:1px solid #e3e8e3"><th>Order</th><th>Date</th><th style="text-align:right">Total</th></tr>
{% for o in orders %}<tr style="border-bottom:1px solid #f0f2f0"><td>#{{ o.id }}</td><td>{{ o.date }}</td><td style="text-align:right">{{ '%.2f' % o.total }} INR</td></tr>
{% endfor %}</table>
{% else %}<p>No orders in the last 30 days. Add something to the cart and check out, then ask for the report again.</p>{% endif %}""")

_REMINDER = _BASE.replace("{% block body %}{% endblock %}", """\
<p style="margin-top:0">Hey, we've been missing you!</p>
<p>Fresh stock is waiting at the store. A few things that are in stock right now:</p>
<ul>{% for p in products %}<li>{{ p.name }} - {{ '%.2f' % p.price }} INR per {{ p.unit }}</li>
{% endfor %}</ul>
<p>See you soon!</p>""")

_TEMPLATES = {name: _env.from_string(src) for name, src in
              (("order_confirmation", _ORDER), ("monthly_report", _REPORT), ("daily_reminder", _REMINDER))}

SUBJECTS = {
    "order_confirmation": "Your Grocery Store demo order #{order_id}",
    "monthly_report": "Your Grocery Store demo monthly report",
    "daily_reminder": "Knock knock: your Grocery Store demo reminder",
}


def render(kind, **context):
    """Return ``(subject, html)`` for a template kind."""
    subject = SUBJECTS[kind].format(order_id=context.get("order_id", ""))
    return subject, _TEMPLATES[kind].render(footer=FOOTER, **context)
