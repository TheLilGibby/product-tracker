import logging
from datetime import datetime, timedelta
from app import db

logger = logging.getLogger(__name__)


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

    def __repr__(self):
        return f'<Product {self.name}>'

    def record_availability(self, now=None):
        """
        Log this listing's stock state if it differs from the last one logged.

        The only writer of AvailabilityHistory. Every path that checks a
        listing (the scheduler, Update Now, the API refresh, adding a product)
        calls it right after setting available and last_checked. The first
        call logs the starting state; after that a row is added only on a
        change, with the price at that moment.

        The comparison is against the newest logged row, not the value the
        caller just overwrote, so a change that reached product.available
        without being logged is still logged by the next check.

        Returns the new row, or None when the state is unchanged.
        """
        available = bool(self.available)
        last = None
        if self.id is not None:
            last = (AvailabilityHistory.query
                    .filter_by(product_id=self.id)
                    .order_by(AvailabilityHistory.timestamp.desc(), AvailabilityHistory.id.desc())
                    .first())
        if last is not None and last.available == available:
            return None
        row = AvailabilityHistory(timestamp=now or datetime.utcnow(), available=available,
                                  price=self.current_price)
        self.availability_histories.append(row)
        if last is not None:
            logger.info(f"Stock changed for product {self.id}: "
                        f"{'in stock' if available else 'out of stock'}")
        return row

    # The ranges the detail-page chart can show, widest last.
    AVAILABILITY_WINDOWS = (
        ('hour', 'Last hour', timedelta(hours=1)),
        ('day', 'Last 24 hours', timedelta(days=1)),
        ('week', 'Last 7 days', timedelta(days=7)),
    )

    def availability_windows(self, now=None):
        """
        The stock timeline split into the ranges the availability chart shows.

        Built from AvailabilityHistory, which holds one row per stock change
        plus the first observation, and closed with last_checked: the state
        held at least until the latest check, so the line runs to it instead
        of stopping at the last change.

        Each window starts with the state carried in from before it, placed at
        the window's start and flagged 'carry', so a window with no change of
        its own is a flat line at that state rather than an empty graph. When
        the listing was not checked inside a window at all, 'checked' is False
        and the carried state is held out to now, also flagged 'carry'.

        Returns one dict per window with 'key', 'label', 'start' and 'end'
        (naive UTC), 'points' (oldest first), 'changes' (stock status flips
        inside the window) and 'checked'.
        """
        now = now or datetime.utcnow()
        floor = now - max(span for _, _, span in self.AVAILABILITY_WINDOWS)

        points = []
        if self.id is not None:
            carry = (AvailabilityHistory.query
                     .filter(AvailabilityHistory.product_id == self.id,
                             AvailabilityHistory.timestamp < floor)
                     .order_by(AvailabilityHistory.timestamp.desc(), AvailabilityHistory.id.desc())
                     .first())
            rows = (AvailabilityHistory.query
                    .filter(AvailabilityHistory.product_id == self.id,
                            AvailabilityHistory.timestamp >= floor)
                    .order_by(AvailabilityHistory.timestamp.asc(), AvailabilityHistory.id.asc())
                    .all())
            if carry is not None:
                rows.insert(0, carry)
            points = [{'timestamp': row.timestamp, 'available': bool(row.available)} for row in rows]
        # Nothing logged means no state is known yet, whatever last_checked says
        # (a listing added without a successful scrape still has one).
        if points and self.last_checked is not None and self.last_checked > points[-1]['timestamp']:
            points.append({'timestamp': self.last_checked, 'available': bool(self.available)})

        windows = []
        for key, label, span in self.AVAILABILITY_WINDOWS:
            start = now - span
            before = [point for point in points if point['timestamp'] < start]
            series = [dict(point, carry=False) for point in points if point['timestamp'] >= start]
            if before:
                series.insert(0, dict(before[-1], timestamp=start, carry=True))
            checked = bool(points) and self.last_checked is not None and self.last_checked >= start
            if series and not checked:
                series.append(dict(series[-1], timestamp=now, carry=True))
            changes = sum(1 for older, newer in zip(series, series[1:])
                          if older['available'] != newer['available'])
            windows.append({
                'key': key,
                'label': label,
                'start': start,
                'end': now,
                'points': series,
                'changes': changes,
                'checked': checked,
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

    The only stock history. Product.record_availability is its only writer and
    every check path calls it. price is the listing's price at that moment, so
    a restock can be read against what it cost. A check that changed nothing
    leaves no row; the listing's last_checked records the latest one.

    It replaced stock_checks, which logged every check without a price. That
    table is retired: see create_db.py.
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
