"""
The availability charts: one listing's stock over time, and one group's.

Both charts draw the same thing over the same three windows - stepped lines on
a real time axis, because the history logs only changes and spacing uneven
points evenly would give an hour-long blip the same width as a quiet day. So
the windows, the tick times and every label are built here once, for both.

Labels are rendered on the server rather than in the browser because the times
are shown in the timezone picked in settings, which the browser has no way to
know. Switching ranges in the page then costs no round trip.

A listing's chart is a single line between "out of stock" and "in stock"
(`availability_chart_ranges`). A group's chart gives every retailer a
horizontal lane of its own and draws that retailer's line inside it
(`group_chart_ranges`): five listings that are all out of stock have to read as
five flat lines, not as one line drawn five times over.
"""
from datetime import datetime, timedelta

import pytz
from flask import current_app, session, url_for

from app.models.product import Product
from app.scrapers import detect_store_type, store_icon_path

# How far apart the x-axis ticks sit in each window. They fall on round local
# times: every 10 minutes, every 3 hours, each midnight.
_TICK_STEPS = {
    'hour': timedelta(minutes=10),
    'day': timedelta(hours=3),
    'week': timedelta(days=1),
}

# Where a lane's two states sit inside the 1.0 of height it owns. The gap above
# and below keeps a line off its lane's edge, so neighbouring lanes stay apart.
_LANE_OUT = 0.18
_LANE_IN = 0.82


def store_icon(url):
    """The retailer tile for a listing URL, as a URL the page can load."""
    return url_for('static', filename=store_icon_path(detect_store_type(url)))


def epoch_ms(moment):
    """Naive UTC as milliseconds since the epoch, the charts' x unit."""
    return int((moment - datetime(1970, 1, 1)).total_seconds() * 1000)


def _clock(moment, twelve_hour, seconds=False):
    """The time of day, written the way a person would say it."""
    if twelve_hour:
        hour = moment.hour % 12 or 12
        text = f"{hour}:{moment.strftime('%M')}"
        if seconds:
            text += f":{moment.strftime('%S')}"
        return f"{text} {moment.strftime('%p')}"
    return moment.strftime('%H:%M:%S' if seconds else '%H:%M')


class _Labels:
    """Times written for display, in the timezone and clock chosen in settings."""

    def __init__(self):
        timezone = session.get('timezone',
                               current_app.config.get('DEFAULT_TIMEZONE', 'UTC'))
        self.twelve_hour = session.get(
            'time_format', current_app.config.get('TIME_FORMAT', '24h')) == '12h'
        self.tz = pytz.timezone(timezone)

    def localize(self, moment):
        if moment.tzinfo is None:
            moment = pytz.utc.localize(moment)
        return moment.astimezone(self.tz)

    def axis(self, key, moment):
        """An x-axis tick label: each window gets the label its span deserves."""
        if key == 'hour':
            return _clock(moment, self.twelve_hour)
        if key == 'day':
            return f"{moment.strftime('%a')} {_clock(moment, self.twelve_hour)}"
        return f"{moment.strftime('%a')} {moment.strftime('%b')} {moment.day}"

    def full(self, moment):
        """The whole moment, for a tooltip."""
        moment = self.localize(moment)
        return (f"{moment.strftime('%a')}, {moment.strftime('%b')} {moment.day}, "
                f"{moment.year} at {_clock(moment, self.twelve_hour, seconds=True)} "
                f"{moment.strftime('%Z')}".strip())

    def ticks(self, key, start, end):
        """Ticks on round local times between start and end (both naive UTC)."""
        wall = self.localize(start).replace(tzinfo=None)
        if key == 'hour':
            wall = wall.replace(minute=wall.minute - wall.minute % 10, second=0, microsecond=0)
        elif key == 'day':
            wall = wall.replace(hour=wall.hour - wall.hour % 3, minute=0, second=0, microsecond=0)
        else:
            wall = wall.replace(hour=0, minute=0, second=0, microsecond=0)
        out = []
        while True:
            # Stepping the wall clock, not UTC, keeps ticks on the hour across DST.
            moment = self.tz.localize(wall)
            at = moment.astimezone(pytz.utc).replace(tzinfo=None)
            if at > end:
                return out
            if at >= start:
                out.append({'v': epoch_ms(at), 'label': self.axis(key, moment)})
            wall += _TICK_STEPS[key]


def availability_chart_ranges(product):
    """
    One listing's chart: its time windows, labelled for display.

    Returns one dict per window with 'key', 'label', 'start'/'end' (epoch ms),
    'ticks', 'changes', 'checked', 'last_checked' and 'points', where a point is
    {'t', 'y' (1 in stock, 0 out), 'full', 'carry'}. Carried points are not
    observations - they hold the line at a window's edges.

    Returns [] when the listing has no stock history in any window.
    """
    labels = _Labels()
    last_checked = labels.full(product.last_checked) if product.last_checked else None
    icon = store_icon(product.url)

    ranges = []
    for window in product.availability_windows():
        start, end = window['start'], window['end']
        points = []
        for point in window['points']:
            full = labels.full(point['timestamp'])
            if point['carry']:
                full += (' (carried in from before this range)'
                         if point['timestamp'] == start else ' (last known state)')
            points.append({
                't': epoch_ms(point['timestamp']),
                'y': 1 if point['available'] else 0,
                'full': full,
                'carry': point['carry'],
            })
        ranges.append({
            'key': window['key'],
            'label': window['label'],
            'start': epoch_ms(start),
            'end': epoch_ms(end),
            'ticks': labels.ticks(window['key'], start, end),
            'changes': window['changes'],
            'checked': window['checked'],
            'last_checked': last_checked,
            'icon': icon,
            'points': points,
        })
    if not any(r['points'] for r in ranges):
        return []
    return ranges


def group_chart_ranges(listings):
    """
    One group's chart: every retailer's stock timeline, a lane each.

    `listings` is what group_listings() returns - [{'product', 'store'}] - and
    lane order follows it, so the chart reads top to bottom in the same order as
    the table above it.

    Every listing's windows are built against one shared `now`, so the lanes
    share an x axis exactly. A lane owns 1.0 of height; its line sits low when
    the listing is out of stock and high when it is in stock, which is what
    makes a restock at one retailer visible while the other four stay flat.

    Returns one dict per window with 'key', 'label', 'start'/'end' (epoch ms),
    'ticks', 'lanes', 'series', and the roll-ups 'changes', 'restocks' and
    'quiet' (how many listings were not checked at all inside the window).
    Returns [] when no listing has any stock history in any window.
    """
    if not listings:
        return []

    labels = _Labels()
    now = datetime.utcnow()
    total = len(listings)

    lanes, per_listing = [], []
    for index, row in enumerate(listings):
        product = row['product']
        # Lane 0 is drawn at the top, so the chart matches the table's order,
        # and y grows upwards in the canvas.
        base = total - 1 - index
        lanes.append({
            'store': row['store'],
            'icon': store_icon(product.url),
            'product_id': product.id,
            'floor': base + _LANE_OUT,
            'ceiling': base + _LANE_IN,
            'mid': base + 0.5,
            'bottom': base,
            'top': base + 1.0,
            'last_checked': labels.full(product.last_checked) if product.last_checked else None,
        })
        per_listing.append(product.availability_windows(now=now))

    ranges = []
    for index, (key, label, span) in enumerate(Product.AVAILABILITY_WINDOWS):
        start, end = now - span, now
        series = []
        for lane_index, windows in enumerate(per_listing):
            window = windows[index]
            lane = lanes[lane_index]
            points = []
            for point in window['points']:
                full = labels.full(point['timestamp'])
                if point['carry']:
                    full += (' (carried in from before this range)'
                             if point['timestamp'] == start else ' (last known state)')
                points.append({
                    't': epoch_ms(point['timestamp']),
                    'y': lane['ceiling'] if point['available'] else lane['floor'],
                    'available': bool(point['available']),
                    'full': full,
                    'carry': point['carry'],
                })
            restocks = sum(1 for older, newer in zip(window['points'], window['points'][1:])
                           if newer['available'] and not older['available'])
            series.append({
                'lane': lane_index,
                'store': lane['store'],
                'icon': lane['icon'],
                'product_id': lane['product_id'],
                'floor': lane['floor'],
                'changes': window['changes'],
                'restocks': restocks,
                'checked': window['checked'],
                'last_checked': lane['last_checked'],
                'points': points,
            })
        ranges.append({
            'key': key,
            'label': label,
            'start': epoch_ms(start),
            'end': epoch_ms(end),
            'ticks': labels.ticks(key, start, end),
            'lanes': lanes,
            'series': series,
            'changes': sum(s['changes'] for s in series),
            'restocks': sum(s['restocks'] for s in series),
            'quiet': sum(1 for s in series if s['points'] and not s['checked']),
            'tracked': total,
        })
    if not any(s['points'] for r in ranges for s in r['series']):
        return []
    return ranges
