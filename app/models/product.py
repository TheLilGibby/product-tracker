from datetime import datetime
from app import db

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
    
    # Discord notification settings
    discord_webhook_url = db.Column(db.String(500), nullable=True)
    notify_on_price_drop = db.Column(db.Boolean, default=True)
    notify_on_availability = db.Column(db.Boolean, default=True)
    
    # Auto cart functionality settings
    auto_cart_enabled = db.Column(db.Boolean, default=False)
    auto_cart_quantity = db.Column(db.Integer, default=1)
    last_cart_attempt = db.Column(db.DateTime, nullable=True)
    last_cart_status = db.Column(db.String(100), nullable=True)
    
    # Relationship with price history
    price_histories = db.relationship('PriceHistory', backref='product', lazy=True, cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Product {self.name}>'
    
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
