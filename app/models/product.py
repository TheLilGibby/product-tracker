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