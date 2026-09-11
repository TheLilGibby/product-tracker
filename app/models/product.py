from datetime import datetime, timedelta
from app import db

def _thin_availability(points, max_points):
    """Drop redundant check-ins, keeping both sides of every status change."""
    if not max_points or len(points) <= max_points:
        return points
    span = (points[-1]['timestamp'] - points[0]['timestamp']).total_seconds()
    step = max(span / max_points, 0.001)
    kept = [points[0]]
    for index in range(1, len(points) - 1):
        point = points[index]
        at_a_change = (point['available'] != points[index - 1]['available']
                       or point['available'] != points[index + 1]['available'])
        if at_a_change or (point['timestamp'] - kept[-1]['timestamp']).total_seconds() >= step:
            kept.append(point)
    kept.append(points[-1])
    return kept


class Product(db.Model):
    """Product model representing items being tracked."""
    __tablename__ = 'products'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    url = db.Column(db.String(500), nullable=False, unique=True)
    image_url = db.Column(db.String(500), nullable=True)
    current_price = db.Column(db.Float, nullable=True)
    target_price = db.Column(db.Float, nullable=True)
    last_checked = db.Column(db.DateTime, default=datetime.utcnow)
    available = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Master on/off switch. Off pauses scheduled checks and auto-cart for this
    # product without losing its history, so it can be resumed instead of deleted.
    tracking_enabled = db.Column(db.Boolean, default=True, nullable=False,
                                 server_default='1')
    
    # Discord notification settings
    discord_webhook_url = db.Column(db.String(500), nullable=True)
    notify_on_price_drop = db.Column(db.Boolean, default=True)
    notify_on_availability = db.Column(db.Boolean, default=True)
    notify_on_cart = db.Column(db.Boolean, default=True)
    
    # Auto cart functionality settings
    auto_cart_enabled = db.Column(db.Boolean, default=False)
    auto_cart_quantity = db.Column(db.Integer, default=1)
    last_cart_attempt = db.Column(db.DateTime, nullable=True)
    last_cart_status = db.Column(db.String(100), nullable=True)
    
    # Relationship with price history
    price_histories = db.relationship('PriceHistory', backref='product', lazy=True, cascade='all, delete-orphan')
    stock_checks = db.relationship(
        'StockCheck',
        backref='product',
        lazy=True,
        cascade='all, delete-orphan',
        order_by='StockCheck.timestamp.asc()',
    )
    
    def __repr__(self):
        return f'<Product {self.name}>'

    def record_stock_check(self, available=None, timestamp=None):
        """Record one tracker check-in for the availability timeline."""
        check = StockCheck(
            available=bool(self.available if available is None else available),
            timestamp=timestamp or datetime.utcnow(),
        )
        self.stock_checks.append(check)
        return check

    def availability_chart_points(self, limit=200):
        """Chronological check-ins for the availability graph (newest-capped)."""
        checks = list(self.stock_checks or [])
        if limit and len(checks) > limit:
            checks = checks[-limit:]
        points = [{'timestamp': c.timestamp, 'available': bool(c.available)} for c in checks]
        if not points:
            if self.created_at is not None:
                points.append({'timestamp': self.created_at, 'available': bool(self.available)})
            if (
                self.last_checked is not None
                and (not points or self.last_checked != points[-1]['timestamp'])
            ):
                points.append({'timestamp': self.last_checked, 'available': bool(self.available)})
            return points
        if (
            len(points) == 1
            and self.created_at is not None
            and self.created_at < points[0]['timestamp']
        ):
            points.insert(0, {
                'timestamp': self.created_at,
                'available': points[0]['available'],
            })
        return points

    # The ranges the detail-page chart can show, widest last.
    AVAILABILITY_WINDOWS = (
        ('hour', 'Last hour', timedelta(hours=1)),
        ('day', 'Last 24 hours', timedelta(days=1)),
        ('week', 'Last 7 days', timedelta(days=7)),
    )

    def availability_windows(self, now=None, max_points=400):
        """
        Check-ins split into the ranges the availability chart can show.

        Returns one entry per window in AVAILABILITY_WINDOWS, oldest point
        first, each with the count of check-ins that fell inside it and the
        number of times the stock status changed there.

        Every window starts with the last check-in from BEFORE it, flagged
        'carry'. Availability is a step function, so a window whose own
        check-ins all predate it is not empty - it is a flat line at whatever
        the last known state was - and without the carry point it would render
        as a blank graph that looks like a broken tracker.

        Windows longer than max_points are thinned, but a point is always kept
        when the status differs from its neighbour on either side, so every
        transition survives at full resolution and the shape of the line is
        exact however many identical check-ins sit between changes.
        """
        now = now or datetime.utcnow()
        floor = now - max(span for _, _, span in self.AVAILABILITY_WINDOWS)

        points = []
        if self.id is not None:
            carry = (StockCheck.query
                     .filter(StockCheck.product_id == self.id,
                             StockCheck.timestamp < floor)
                     .order_by(StockCheck.timestamp.desc())
                     .first())
            if carry is not None:
                points.append({'timestamp': carry.timestamp,
                               'available': bool(carry.available)})
            points.extend(
                {'timestamp': check.timestamp, 'available': bool(check.available)}
                for check in (StockCheck.query
                              .filter(StockCheck.product_id == self.id,
                                      StockCheck.timestamp >= floor)
                              .order_by(StockCheck.timestamp.asc())
                              .all())
            )
        if not points:
            # No check-in rows at all: fall back to whatever the product itself
            # can say about when it was created and last looked at.
            points = self.availability_chart_points()

        windows = []
        for key, label, span in self.AVAILABILITY_WINDOWS:
            start = now - span
            inside = [point for point in points if point['timestamp'] >= start]
            before = [point for point in points if point['timestamp'] < start]

            series = [dict(point, carry=False) for point in inside]
            if before:
                series.insert(0, dict(before[-1], carry=True))

            changes = 0
            for index in range(1, len(series)):
                if series[index]['available'] != series[index - 1]['available']:
                    changes += 1

            windows.append({
                'key': key,
                'label': label,
                'points': _thin_availability(series, max_points),
                'checks': len(inside),
                'changes': changes,
            })
        return windows

    @property
    def store(self):
        """Store key derived from the product URL (e.g. 'amazon'), or None."""
        from app.scrapers import detect_store_type
        return detect_store_type(self.url)

    @property
    def store_label(self):
        """Human-readable retailer name for tables and badges."""
        from app.scrapers import store_label_from_url
        return store_label_from_url(self.url)
    
    @property
    def price_history_data(self):
        """Return price history formatted for charts"""
        return {
            'dates': [h.timestamp.strftime('%Y-%m-%d %H:%M') for h in self.price_histories],
            'prices': [h.price for h in self.price_histories]
        }
    
    @property
    def last_price_change(self):
        """Get the date of the last price change"""
        if len(self.price_histories) >= 2:
            return self.price_histories[0].timestamp
        return self.created_at

class PriceHistory(db.Model):
    """Model to track price changes over time."""
    __tablename__ = 'price_histories'
    
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    price = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<PriceHistory ${self.price} @ {self.timestamp}>'


# Product groups. A Product row is one retailer's listing and stays the unit the
# scheduler checks and auto-cart fires on. A group only says which listings are
# the same real item, e.g. the Zelda Switch 2 console at Target, Best Buy and
# Walmart. Membership lives in its own table, not a column on products, so an
# existing database needs no ALTER: db.create_all() adds these tables and a
# products table that has never seen them keeps working.
#
# The relationships onto Product are declared from this side as backrefs, so
# the Product class itself is unchanged.

class ProductGroup(db.Model):
    """One real product, sold by one or more retailers."""
    __tablename__ = 'product_groups'

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Deleting a group removes its memberships, never the listings.
    members = db.relationship('ProductGroupMember', back_populates='group',
                              cascade='all, delete-orphan')

    def __repr__(self):
        return f'<ProductGroup {self.name}>'


class ProductGroupMember(db.Model):
    """Puts one listing in one group. product_id is the key, so a listing has at most one group."""
    __tablename__ = 'product_group_members'

    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('product_groups.id'), nullable=False, index=True)

    group = db.relationship('ProductGroup', back_populates='members')
    # Deleting a listing removes its membership; setting
    # product.group_membership = None removes it too.
    product = db.relationship('Product', backref=db.backref(
        'group_membership', uselist=False, cascade='all, delete-orphan'))

    def __repr__(self):
        return f'<ProductGroupMember product={self.product_id} group={self.group_id}>'


class AvailabilityHistory(db.Model):
    """
    One row per observed stock change on a listing, plus its first observation.

    Written in exactly one place, check_all_products in app/tasks.py. price is
    the listing's price at that moment, so a restock can be read against what
    it cost.
    """
    __tablename__ = 'availability_histories'
    __table_args__ = (
        db.Index('ix_availability_histories_product_time', 'product_id', 'timestamp'),
    )

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    available = db.Column(db.Boolean, nullable=False)
    price = db.Column(db.Float, nullable=True)

    product = db.relationship('Product', backref=db.backref(
        'availability_histories', lazy=True, cascade='all, delete-orphan',
        order_by='AvailabilityHistory.timestamp.desc()'))

    def __repr__(self):
        state = 'in stock' if self.available else 'out of stock'
        return f'<AvailabilityHistory product={self.product_id} {state} @ {self.timestamp}>'
class StockCheck(db.Model):
    """One stock check-in from the product tracker timeline."""
    __tablename__ = 'stock_checks'

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False, index=True)
    available = db.Column(db.Boolean, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    def __repr__(self):
        status = 'in stock' if self.available else 'out of stock'
        return f'<StockCheck {status} @ {self.timestamp}>' 
