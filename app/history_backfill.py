"""
One-off carry-over from the retired stock_checks table.

Until 2026-09-11 there were two stock histories: stock_checks, one row per
check with no price, and availability_histories, one row per stock change with
the price at the time. availability_histories is now the only one. The
stock_checks rows stay where they are and nothing writes to them any more.
This copies the stock changes they recorded before a listing's first
availability_histories row, so the detail chart and the group page keep that
stretch.

Safe to run on every start (create_db.py does). Once a listing's history
reaches back to its first stock check there is nothing older left to copy, and
a database without stock_checks is left alone.
"""
from sqlalchemy import Boolean, DateTime, Float, Integer, column, inspect, text

from app import db
from app.models.product import AvailabilityHistory

# Checks older than every availability_histories row of their listing, oldest
# first, with the price the listing had at that moment. A check from before the
# listing was created belonged to an earlier product that had the same id, so
# it is skipped.
_EARLIER_CHECKS = text("""
    SELECT s.product_id, s.timestamp, s.available,
           (SELECT h.price FROM price_histories h
             WHERE h.product_id = s.product_id AND h.timestamp <= s.timestamp
             ORDER BY h.timestamp DESC LIMIT 1) AS price
      FROM stock_checks s
      JOIN products p ON p.id = s.product_id
     WHERE (p.created_at IS NULL OR s.timestamp >= p.created_at)
       AND NOT EXISTS (SELECT 1 FROM availability_histories a
                        WHERE a.product_id = s.product_id AND a.timestamp <= s.timestamp)
     ORDER BY s.product_id, s.timestamp, s.id
""").columns(column('product_id', Integer), column('timestamp', DateTime),
             column('available', Boolean), column('price', Float))


def backfill_from_stock_checks():
    """Copy the stock changes stock_checks saw before each listing's logged history. Returns rows added."""
    tables = set(inspect(db.engine).get_table_names())
    if not {'stock_checks', 'availability_histories', 'price_histories'} <= tables:
        return 0

    added = 0
    last = {}
    for product_id, timestamp, available, price in db.session.execute(_EARLIER_CHECKS).all():
        available = bool(available)
        if last.get(product_id) == available:
            continue
        last[product_id] = available
        db.session.add(AvailabilityHistory(product_id=product_id, timestamp=timestamp,
                                           available=available, price=price))
        added += 1
    db.session.commit()
    return added
